# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Check CrashManager for reducible crashes, and queue them in Taskcluster.
"""


import argparse
import os
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from logging import WARNING, getLogger
from pathlib import Path
from random import choice, random
from string import Template
from time import time
from typing import Dict, Generator, List, Optional, Tuple

from dateutil.parser import isoparse
from grizzly.common.reporter import Quality
from taskcluster.exceptions import TaskclusterFailure
from taskcluster.utils import slugId, stringDate
from yaml import safe_load as yaml_load

from .common import (
    CommonArgParser,
    CrashManager,
    ReductionWorkflow,
    Taskcluster,
    format_seconds,
)

LOG = getLogger(__name__)

# GENERIC_PLATFORM is used as a first pass for all unreduced test cases.
GENERIC_PLATFORM = "linux"

TC_QUEUES = {
    # "android": "grizzly-reduce-worker-android",
    "linux": "grizzly-reduce-worker",
    # "macosx": "grizzly-reduce-worker-macos",
    "windows": "grizzly-reduce-worker-windows",
}

TOOL_LIST_SECRET = "project/fuzzing/grizzly-reduce-tool-list"
OWNER_EMAIL = "truber@mozilla.com"
REDUCTION_MAX_RUN_TIME = timedelta(hours=6)
REDUCTION_DEADLINE = timedelta(days=1)
REDUCTION_EXPIRES = timedelta(weeks=2)
SCHEDULER_ID = "fuzzing"
PROVISIONER_ID = "proj-fuzzing"
DESCRIPTION = """*DO NOT EDIT* - This resource is configured automatically.

Fuzzing workers generated by decision task"""
RANDOMIZE_CRASH_SELECT = 0.25  # randomly ignore testcase size & ID when selecting
TEMPLATES = (Path(__file__).parent / "task_templates").resolve()
REDUCE_TASKS = {
    "linux": Template((TEMPLATES / "reduce.yaml").read_text()),
    "android": Template((TEMPLATES / "reduce-android.yaml").read_text()),
    "macosx": Template((TEMPLATES / "reduce-macos.yaml").read_text()),
    "windows": Template((TEMPLATES / "reduce-windows.yaml").read_text()),
}


ReducibleCrash = namedtuple(
    "ReducibleCrash", "crash, bucket, tool, description, os, quality"
)


def _fuzzmanager_get_crashes(
    tool_list: List[str],
) -> Generator[ReducibleCrash, None, None]:
    """This function is responsible for getting CrashInfo objects to try to reduce
    from FuzzManager.

    Yields all crashes where:
        quality = 4 OR quality = 5 OR quality = 6
        AND
        tool is in tool_list
        AND
            crash is unbucketed
            OR
            bucket quality is 5 or 6

    Note that a bucket may have a quality=0 testcase from a tool which isn't in
    tool_list, and this would prevent any other testcases in the bucket
    from, being reduced.

    Arguments:
        tool_list: List of tools to monitor for reduction.

    Yields:
        All the info needed to queue a crash for reduction
    """
    assert isinstance(tool_list, list)
    srv = CrashManager()
    crashentry_cutoff = datetime.now(timezone.utc) - timedelta(days=60)

    # get map of bucket ID/tool -> shortDescription for buckets with best testcase
    # having specified quality
    bucket_descs = {}
    bucket_tools = set()
    for tool in tool_list:
        LOG.debug("retrieving buckets for tool %s", tool)

        for bucket in srv.list_buckets(
            {
                "op": "AND",
                "crashentry__tool__name": tool,
                "doNotReduce": False,
            }
        ):
            if bucket["best_quality"] in {
                Quality.ORIGINAL.value,
                Quality.REQUEST_SPECIFIC.value,
                Quality.UNREDUCED.value,
            }:
                bucket_descs[bucket["id"]] = bucket["shortDescription"]
                bucket_tools.add((bucket["id"], tool))
                LOG.debug(
                    "adding bucket %d (%s) Q%d",
                    bucket["id"],
                    tool,
                    bucket["best_quality"],
                )

    # for each bucket+tool, get crashes with specified quality
    LOG.debug(
        "retrieving crashes for %d buckets (%d bucket/tools)",
        len(bucket_descs),
        len(bucket_tools),
    )

    buckets_by_tool: Dict[str, List[str]] = {}
    for bucket, tool in bucket_tools:
        buckets_by_tool.setdefault(tool, [])
        buckets_by_tool[tool].append(bucket)
    for tool, bucket_filter in buckets_by_tool.items():
        LOG.debug("retrieving %d buckets for tool %s", len(bucket_filter), tool)
        for crash in srv.list_crashes(
            {
                "op": "AND",
                "tool__name": tool,
                "bucket_id__in": bucket_filter,
                "created__gt": crashentry_cutoff.isoformat(),
                "testcase__quality__in": [
                    Quality.IGNORED.value,
                    Quality.REDUCING.value,
                    Quality.REQUEST_SPECIFIC.value,
                    Quality.UNREDUCED.value,
                ],
            },
            ordering=["testcase__size", "-id"],
        ):
            yield ReducibleCrash(
                crash=crash["id"],
                bucket=crash["bucket"],
                tool=crash["tool"],
                description=bucket_descs[crash["bucket"]],
                os=crash["os"],
                quality=crash["testcase_quality"],
            )

    # get unbucketed crashes with specified quality
    # include Q0/Q4 so we can limit these just like bucketed
    LOG.debug("retrieving unbucketed crashes")
    for crash in srv.list_crashes(
        {
            "op": "AND",
            "bucket__isnull": True,
            # Quality.ORIGINAL is not in this list for a reason
            # this probably means the REDUCED crash went into a bucket
            # or the description changed. we want to keep trying those
            "testcase__quality__in": [
                Quality.IGNORED.value,
                Quality.REDUCED.value,
                Quality.REDUCING.value,
                Quality.REQUEST_SPECIFIC.value,
                Quality.UNREDUCED.value,
            ],
            "tool__name__in": tool_list,
        },
        ordering=["testcase__size", "-id"],
    ):
        if (
            crash["testcase_quality"]
            in {
                Quality.UNREDUCED.value,
                Quality.REQUEST_SPECIFIC.value,
            }
            and isoparse(crash["created"]) < crashentry_cutoff
        ):
            continue
        yield ReducibleCrash(
            crash=crash["id"],
            bucket=None,
            tool=crash["tool"],
            description=crash["shortSignature"],
            os=crash["os"],
            quality=crash["testcase_quality"],
        )


def _filter_reducing_unbucketed(
    tool_list: List[str],
) -> Generator[ReducibleCrash, None, None]:
    """This function calls `_fuzzmanager_get_crashes` and filters unbucketed
    tool/shortSignature crashes if any are reducing already.

    Arguments:
        tool_list: List of tools to monitor for reduction.

    Yields:
        Description and all info needed to queue a crash for reduction
    """
    # dict of tag -> min quality where we have a quality < UNREDUCED
    skip: Dict[Tuple[Optional[int], str, str], int] = {}
    # dict of tag -> list of crashes, for crashes where a reducing crash has not been
    # seen yet
    queue: Dict[Tuple[Optional[int], str, str], List[ReducibleCrash]] = {}

    for crash in _fuzzmanager_get_crashes(tool_list):
        tag = (crash.bucket, crash.tool, crash.description)
        if tag in skip:
            skip[tag] = min(crash.quality, skip[tag])
        else:
            if crash.quality < Quality.UNREDUCED.value:
                skip[tag] = crash.quality
                queue.pop(tag, None)
            else:
                queue.setdefault(tag, [])
                queue[tag].append(crash)

    for (bucket, tool, description), quality in skip.items():
        if quality == Quality.REDUCING.value:
            reason = "reducing"
        elif quality == Quality.IGNORED.value:
            reason = "marked do-not-reduce"
        elif quality == Quality.REDUCED.value:
            # hide output for bucketed Q0 ... this is expected case
            # and doesn't help debugging
            if bucket is not None:
                continue
            reason = "reduced test exists"
        else:
            # Q1 is not expected here, as Q1 should always have a corresponding Q0
            reason = f"unexpected quality {quality!r}"

        LOG.warning(
            "skipped: tool=%s/bucket=%r/desc=%s (%s)",
            tool,
            bucket,
            description,
            reason,
        )

    # we got all crashes and haven't seen any reducing for these shortDescriptions
    for crashes in queue.values():
        yield from crashes


def _get_unique_crashes(
    tool_list: List[str],
) -> Generator[Tuple[str, ReducibleCrash], None, None]:
    """This function calls `_filter_reducing_unbucketed` and picks one unique result
    per bucket/shortSignature to reduce.

    Arguments:
        tool_list: List of tools to monitor for reduction.

    Yields:
        Description and all info needed to queue a crash for reduction
    """
    # set of all crash signatures processed already
    seen = set()
    # map of crash signature -> list of ReducibleCrash objects
    # that have been selected for randomization
    randomize_crashes = {}

    for crash in _filter_reducing_unbucketed(tool_list):
        sig = f"{crash.tool}:{crash.bucket!r}:{crash.description}:{crash.quality!r}"
        if sig not in seen:
            seen.add(sig)
            # maybe don't yield this sig right away, but save all possible sigs
            # and return one at random
            if random() < RANDOMIZE_CRASH_SELECT:
                randomize_crashes[sig] = [crash]
            else:
                yield (sig, crash)
        elif sig in randomize_crashes:
            randomize_crashes[sig].append(crash)
    for sig, crashes in randomize_crashes.items():
        if len(crashes) > 1:
            LOG.info("picking from %d possible crashes for %s", len(crashes), sig)
        yield (sig, choice(crashes))


class ReductionMonitor(ReductionWorkflow):
    """Scan CrashManager to see if there are any crashes which should be reduced.

    Attributes:
        dry_run (bool): Scan CrashManager, but don't queue any work in Taskcluster
                        nor update any state in CrashManager.
        tool_list (list(str)): List of Grizzly tools to look for crashes under.
    """

    def __init__(
        self, dry_run: bool = False, tool_list: Optional[List[str]] = None
    ) -> None:
        super().__init__()
        self.dry_run = dry_run
        self._gw_image_artifact_tasks: Dict[str, str] = {}
        if self.dry_run:
            LOG.warning("*** DRY RUN -- SIMULATION ONLY ***")
        if not tool_list:
            LOG.warning("No tools specified on CLI, fetching from Taskcluster")
            tool_list = Taskcluster.load_secrets(TOOL_LIST_SECRET)["tools"]
        self.tool_list = list(tool_list or [])

    def image_artifact_task(self, namespace):
        if namespace not in self._gw_image_artifact_tasks:
            idx = Taskcluster.get_service("index")
            result = idx.findTask(namespace)
            self._gw_image_artifact_tasks[namespace] = result["taskId"]
        return self._gw_image_artifact_tasks[namespace]

    def queue_reduction_task(
        self, os_name: str, crash_id: int, no_repro_quality: Optional[int]
    ) -> None:
        """Queue a reduction task in Taskcluster.

        Arguments:
            os_name: The OS to schedule the task for.
            crash_id: The CrashManager crash ID to reduce.
            no_repro_quality: testcase Quality to set in FM if crash doesn't repro
        """
        if self.dry_run:
            return None
        dest_queue = TC_QUEUES[os_name]
        my_task_id = os.environ.get("TASK_ID")
        task_id = slugId()
        now = datetime.now(timezone.utc)
        if os_name == "windows":
            image_task_id = self.image_artifact_task(
                "project.fuzzing.orion.grizzly-win.master"
            )
        elif os_name == "macosx":
            image_task_id = self.image_artifact_task(
                "project.fuzzing.orion.grizzly-macos.master"
            )
        else:
            image_task_id = None
        task = yaml_load(
            REDUCE_TASKS[os_name].substitute(
                crash_id=crash_id,
                created=stringDate(now),
                deadline=stringDate(now + REDUCTION_DEADLINE),
                description=DESCRIPTION,
                expires=stringDate(now + REDUCTION_EXPIRES),
                image_task_id=image_task_id,
                max_run_time=int(REDUCTION_MAX_RUN_TIME.total_seconds()),
                os_name=os_name,
                owner_email=OWNER_EMAIL,
                provisioner=PROVISIONER_ID,
                scheduler=SCHEDULER_ID,
                task_group=my_task_id,
                worker=dest_queue,
            )
        )
        if no_repro_quality is not None:
            task["payload"]["env"]["NO_REPRO_QUALITY"] = str(no_repro_quality)
        queue = Taskcluster.get_service("queue")
        LOG.info("Creating task %s: %s", task_id, task["metadata"]["name"])
        try:
            queue.createTask(task_id, task)
        except TaskclusterFailure as exc:
            LOG.error("Error creating task: %s", exc)
            return None
        LOG.info("Marking %d Q4 (in progress)", crash_id)
        CrashManager().update_testcase_quality(crash_id, Quality.REDUCING.value)

    def run(self) -> Optional[int]:
        start_time = time()
        srv = CrashManager()

        LOG.info("starting poll of FuzzManager")
        # mark all Q=6 crashes that we don't have a queue for, move them to Q=10
        for crash in srv.list_crashes(
            {
                "op": "AND",
                "testcase__quality": Quality.REQUEST_SPECIFIC.value,
                "tool__name__in": list(self.tool_list),
                "_": {
                    "op": "NOT",
                    "os__name__in": list(set(TC_QUEUES) - {GENERIC_PLATFORM}),
                },
            }
        ):
            LOG.info(
                "crash %d updating Q%d => Q%d, platform is %s",
                crash["id"],
                Quality.REQUEST_SPECIFIC.value,
                Quality.NOT_REPRODUCIBLE.value,
                crash["os"],
            )
            if not self.dry_run:
                srv.update_testcase_quality(crash["id"], Quality.NOT_REPRODUCIBLE.value)

        # get all crashes for Q=5 and project in tool_list
        queued = 0
        for sig, reduction in _get_unique_crashes(self.tool_list):
            LOG.info("queuing %d for %s", reduction.crash, sig)
            no_repro_quality = None
            if reduction.quality == Quality.UNREDUCED.value:
                # perform first pass with generic platform reducer on Q5
                os_name = GENERIC_PLATFORM
                if reduction.os in TC_QUEUES and reduction.os != GENERIC_PLATFORM:
                    no_repro_quality = Quality.REQUEST_SPECIFIC.value
            elif reduction.os in TC_QUEUES and reduction.os != GENERIC_PLATFORM:
                # move Q6 to platform specific queue if it exists
                os_name = reduction.os
            else:
                LOG.info(
                    "> updating Q%d => Q%d, platform is %s",
                    reduction.quality,
                    Quality.NOT_REPRODUCIBLE.value,
                    reduction.os,
                )
                if not self.dry_run:
                    srv.update_testcase_quality(
                        reduction.crash, Quality.NOT_REPRODUCIBLE.value
                    )
                continue
            queued += 1
            self.queue_reduction_task(os_name, reduction.crash, no_repro_quality)
        LOG.info(
            "finished polling FuzzManager (%s elapsed, %d tasks queued)",
            format_seconds(time() - start_time),
            queued,
        )
        return 0

    @staticmethod
    def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
        parser = CommonArgParser(prog="grizzly-reduce-tc-monitor")
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="Don't schedule tasks or update crashes in CrashManager, only "
            "print what would be done.",
        )
        parser.add_argument(
            "--tool-list", nargs="+", help="Tools to search for reducible crashes"
        )
        return parser.parse_args(args=args)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "ReductionMonitor":
        return cls(dry_run=args.dry_run, tool_list=args.tool_list)


if __name__ == "__main__":
    getLogger("mohawk").setLevel(WARNING)
    getLogger("taskcluster").setLevel(WARNING)
    getLogger("urllib3").setLevel(WARNING)
    sys.exit(ReductionMonitor.main())
