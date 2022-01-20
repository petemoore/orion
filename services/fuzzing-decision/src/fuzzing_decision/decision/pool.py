# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from string import Template

import dateutil.parser
import yaml
from taskcluster.exceptions import TaskclusterFailure, TaskclusterRestFailure
from taskcluster.utils import fromNow, slugId, stringDate
from tcadmin.resources import Hook, Role, WorkerPool

from ..common import taskcluster
from ..common.pool import PoolConfigMap as CommonPoolConfigMap
from ..common.pool import PoolConfiguration as CommonPoolConfiguration
from ..common.pool import parse_time
from . import (
    CANCEL_TASK_DAYS,
    DECISION_TASK_SECRET,
    HOOK_PREFIX,
    OWNER_EMAIL,
    PROVIDER_IDS,
    PROVISIONER_ID,
    SCHEDULER_ID,
    WORKER_POOL_PREFIX,
)

LOG = logging.getLogger(__name__)

DESCRIPTION = """*DO NOT EDIT* - This resource is configured automatically.

Fuzzing resources generated by https://github.com/MozillaSecurity/orion/tree/master/services/fuzzing-decision"""  # noqa

DOCKER_WORKER_DEVICES = (
    "cpu",
    "hostSharedMemory",
    "loopbackAudio",
    "loopbackVideo",
    "kvm",
)

TEMPLATES = (Path(__file__).parent / "task_templates").resolve()
DECISION_TASK = Template((TEMPLATES / "decision.yaml").read_text())
FUZZING_TASK = Template((TEMPLATES / "fuzzing.yaml").read_text())


class MountArtifactResolver:
    CACHE = {}  # cache of orion service -> taskId

    @classmethod
    def lookup_taskid(cls, namespace):
        assert isinstance(namespace, str)
        if namespace not in cls.CACHE:
            # need to resolve "image" to a task ID where the mount
            # artifact is
            idx = taskcluster.get_service("index")
            result = idx.findTask(namespace)
            cls.CACHE[namespace] = result["taskId"]

        return cls.CACHE[namespace]


def add_task_image(task, config):
    """Add image or mount to task payload, depending on platform."""
    if config.platform in {"macos", "windows"}:
        assert isinstance(config.container, dict)
        assert config.container["type"] != "docker-image"
        if config.container["type"] == "indexed-image":
            # need to resolve "image" to a task ID where the mount artifact is
            task_id = MountArtifactResolver.lookup_taskid(config.container["namespace"])
        else:
            task_id = config.container["taskId"]
        task["payload"].setdefault("mounts", [])
        suffixes = Path(config.container["path"]).suffixes
        if len(suffixes) >= 2 and suffixes[-2] == ".tar":
            fmt = f"tar{suffixes[-1]}"
        else:
            assert suffixes, "unabled to determine format from container path"
            fmt = suffixes[-1][1:]
        task["payload"]["mounts"].append(
            {
                "format": fmt,
                "content": {
                    "taskId": task_id,
                    "artifact": config.container["path"],
                },
                "directory": ".",
            }
        )
        task["dependencies"].append(task_id)
    else:
        # `container` can be either a string or a dict, so can't template it
        task["payload"]["image"] = config.container


def add_capabilities_for_scopes(task):
    """Request capabilities to match the scopes specified by the task"""
    capabilities = task["payload"].setdefault("capabilities", {})
    scopes = set(task["scopes"])
    capabilities.setdefault("devices", {})
    for device in DOCKER_WORKER_DEVICES:
        if f"docker-worker:capability:device:{device}" in scopes:
            capabilities["devices"][device] = True
    if "docker-worker:capability:privileged" in scopes:
        capabilities["privileged"] = True
    if not capabilities["devices"]:
        del capabilities["devices"]
    if not capabilities:
        del task["payload"]["capabilities"]


def configure_task(task, config, now, env):
    task["payload"]["artifacts"].update(
        config.artifact_map(stringDate(fromNow("4 weeks", now)))
    )
    task["scopes"] = sorted(chain(config.get_scopes(), task["scopes"]))
    add_capabilities_for_scopes(task)
    add_task_image(task, config)
    if config.platform == "windows":
        task["payload"]["env"]["MSYSTEM"] = "MINGW64"
        task["payload"]["command"] = [
            "set HOME=%CD%",
            "set ARTIFACTS=%CD%",
            "set PATH="
            + ";".join(
                [
                    r"%CD%\msys64\opt\python",
                    r"%CD%\msys64\opt\python\Scripts",
                    r"%CD%\msys64\MINGW64\bin",
                    r"%CD%\msys64\usr\bin",
                    "%PATH%",
                ]
            ),
            "fuzzing-pool-launch",
        ]
        if config.run_as_admin:
            task["payload"].setdefault("osGroups", [])
            task["payload"]["osGroups"].append("Administrators")
            task["payload"]["features"]["runAsAdministrator"] = True
    elif config.platform == "macos":
        task["payload"]["command"] = [
            [
                "/bin/bash",
                "-c",
                "-x",
                'eval "$(homebrew/bin/brew shellenv)" && exec fuzzing-pool-launch',
            ],
        ]

    if config.platform in {"macos", "windows"}:
        # translate artifacts from dict to array for generic-worker
        task["payload"]["artifacts"] = [
            # `... or artifact` because dict.update returns None
            artifact.update({"name": name}) or artifact
            for name, artifact in task["payload"]["artifacts"].items()
        ]
    if env is not None:
        assert set(task["payload"]["env"]).isdisjoint(set(env))
        task["payload"]["env"].update(env)


def cancel_tasks(worker_type):
    # Avoid cancelling self
    self_task_id = os.getenv("TASK_ID")

    hooks = taskcluster.get_service("hooks")
    queue = taskcluster.get_service("queue")

    # cycle only hook fires after this limit
    cycle_limit = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(
        days=CANCEL_TASK_DAYS
    )

    # Get tasks by hook
    def iter_tasks_by_hook(hook_id):
        try:
            for fire in hooks.listLastFires(HOOK_PREFIX, hook_id)["lastFires"]:
                if fire["result"] != "success":
                    continue
                task_created_time = dateutil.parser.isoparse(fire["taskCreateTime"])
                if task_created_time < cycle_limit:
                    continue
                try:
                    result = queue.listTaskGroup(fire["taskId"])
                except TaskclusterFailure as exc:
                    if "No task-group with taskGroupId" in str(exc):
                        continue
                    raise
                while result.get("continuationToken"):
                    yield from [
                        (task, (fire["firedBy"] == "schedule"))
                        for task in result["tasks"]
                    ]
                    result = queue.listTaskGroup(
                        fire["taskId"],
                        query={"continuationToken": result["continuationToken"]},
                    )
                yield from [
                    (task, (fire["firedBy"] == "schedule")) for task in result["tasks"]
                ]
        except TaskclusterRestFailure as msg:
            if "No such hook" in str(msg):
                return
            raise

    tasks_to_cancel = []
    for task, scheduled in iter_tasks_by_hook(worker_type):
        task_id = task["status"]["taskId"]

        if task_id == self_task_id:
            if scheduled:
                # if this decision task was the result of a scheduled hook, don't
                # cancel anything. if cycle_time is shorter than max_run_time, we
                # want prior tasks to remain running
                LOG.info(f"{self_task_id} is scheduled, not cancelling tasks")
                return
            # avoid cancelling self
            continue

        # State can be pending,running,completed,failed,exception
        # We only cancel pending & running tasks
        if any(
            run["state"] in {"pending", "running"} for run in task["status"]["runs"]
        ):
            tasks_to_cancel.append(task_id)
    LOG.info(f"{self_task_id} is cancelling {len(tasks_to_cancel)} tasks")

    for task_id in tasks_to_cancel:
        # Cancel the task
        try:
            LOG.warning(f"=> cancelling: {task_id}")
            queue.cancelTask(task_id)
        except Exception:
            LOG.exception(f"Exception calling cancelTask({task_id})")


class PoolConfiguration(CommonPoolConfiguration):
    @property
    def task_id(self):
        return f"{self.platform}-{self.pool_id}"

    def get_scopes(self):
        result = self.scopes.copy()
        if self.platform == "windows" and self.run_as_admin:
            result.extend(
                (
                    (
                        "generic-worker:"
                        f"os-group:{PROVISIONER_ID}/{self.task_id}/Administrators"
                    ),
                    (
                        "generic-worker:"
                        f"run-as-administrator:{PROVISIONER_ID}/{self.task_id}"
                    ),
                )
            )
        return result

    def build_resources(self, providers, machine_types, env=None):
        """Build the full tc-admin resources to compare and build the pool"""

        # Select a cloud provider according to configuration
        assert self.cloud in providers, f"Cloud Provider {self.cloud} not available"
        provider = providers[self.cloud]

        # Build the pool configuration for selected machines
        machines = self.get_machine_list(machine_types)
        config = {
            "launchConfigs": provider.build_launch_configs(
                self.imageset, machines, self.disk_size, self.platform
            ),
            "maxCapacity": (
                # * 2 since Taskcluster seems to not reuse workers very quickly in some
                # cases, so we end up with a lot of pending tasks.
                max(1, math.ceil(self.max_run_time / self.cycle_time))
                * self.tasks
                * 2
            ),
            "minCapacity": 0,
        }
        if self.platform == "linux":
            config["lifecycle"] = {
                # give workers 15 minutes to register before assuming they're broken
                "registrationTimeout": parse_time("15m"),
                "reregistrationTimeout": parse_time("4d"),
            }

        # Build the decision task payload that will trigger the new fuzzing tasks
        decision_task = yaml.safe_load(
            DECISION_TASK.substitute(
                description=DESCRIPTION.replace("\n", "\\n"),
                max_run_time=parse_time("1h"),
                owner_email=OWNER_EMAIL,
                pool_id=self.pool_id,
                provisioner=PROVISIONER_ID,
                scheduler=SCHEDULER_ID,
                secret=DECISION_TASK_SECRET,
                task_id=self.task_id,
            )
        )
        decision_task["scopes"] = sorted(
            chain(decision_task["scopes"], self.get_scopes())
        )
        add_capabilities_for_scopes(decision_task)
        if env is not None:
            assert set(decision_task["payload"]["env"]).isdisjoint(set(env))
            decision_task["payload"]["env"].update(env)

        if self.cloud != "static":
            yield WorkerPool(
                config=config,
                description=DESCRIPTION,
                emailOnError=True,
                owner=OWNER_EMAIL,
                providerId=PROVIDER_IDS[self.cloud],
                workerPoolId=f"{WORKER_POOL_PREFIX}/{self.task_id}",
            )

        yield Hook(
            bindings=(),
            description=DESCRIPTION,
            emailOnError=True,
            hookGroupId=HOOK_PREFIX,
            hookId=self.task_id,
            name=self.task_id,
            owner=OWNER_EMAIL,
            schedule=list(self.cycle_crons()),
            task=decision_task,
            triggerSchema={},
        )

        yield Role(
            description=DESCRIPTION,
            roleId=f"hook-id:{HOOK_PREFIX}/{self.task_id}",
            scopes=decision_task["scopes"]
            + ["queue:create-task:highest:proj-fuzzing/ci"],
        )

    def artifact_map(self, expires):
        result = {}
        for local_path, value in self.artifacts.items():
            assert isinstance(value["url"], str)
            assert value["type"] in {"directory", "file"}
            result[value["url"]] = {
                "expires": expires,
                "path": local_path,
                "type": value["type"],
            }
        # this artifact is required by pool_launch
        result["project/fuzzing/private/logs"] = {
            "expires": expires,
            "path": "/logs/" if self.platform == "linux" else "logs",
            "type": "directory",
        }
        return result

    def build_tasks(self, parent_task_id, env=None):
        """Create fuzzing tasks and attach them to a decision task"""
        now = datetime.utcnow()
        preprocess_task_id = None

        preprocess = self.create_preprocess()
        if preprocess is not None:
            task = yaml.safe_load(
                FUZZING_TASK.substitute(
                    created=stringDate(now),
                    deadline=stringDate(
                        now + timedelta(seconds=preprocess.max_run_time)
                    ),
                    description=DESCRIPTION.replace("\n", "\\n"),
                    expires=stringDate(fromNow("4 weeks", now)),
                    max_run_time=preprocess.max_run_time,
                    name=f"Fuzzing task {self.task_id} - preprocess",
                    owner_email=OWNER_EMAIL,
                    pool_id=self.pool_id,
                    provisioner=PROVISIONER_ID,
                    scheduler=SCHEDULER_ID,
                    secret=DECISION_TASK_SECRET,
                    task_group=parent_task_id,
                    task_id=self.task_id,
                )
            )
            task["payload"]["env"]["TASKCLUSTER_FUZZING_PREPROCESS"] = "1"
            configure_task(task, preprocess, now, env)
            preprocess_task_id = slugId()
            yield preprocess_task_id, task

        for i in range(1, self.tasks + 1):
            task = yaml.safe_load(
                FUZZING_TASK.substitute(
                    created=stringDate(now),
                    deadline=stringDate(now + timedelta(seconds=self.max_run_time)),
                    description=DESCRIPTION.replace("\n", "\\n"),
                    expires=stringDate(fromNow("4 weeks", now)),
                    max_run_time=self.max_run_time,
                    name=f"Fuzzing task {self.task_id} - {i}/{self.tasks}",
                    owner_email=OWNER_EMAIL,
                    pool_id=self.pool_id,
                    provisioner=PROVISIONER_ID,
                    scheduler=SCHEDULER_ID,
                    secret=DECISION_TASK_SECRET,
                    task_group=parent_task_id,
                    task_id=self.task_id,
                )
            )
            if preprocess_task_id is not None:
                task["dependencies"].append(preprocess_task_id)
            configure_task(task, self, now, env)
            yield slugId(), task


class PoolConfigMap(CommonPoolConfigMap):
    RESULT_TYPE = PoolConfiguration

    @property
    def task_id(self):
        return f"{self.platform}-{self.pool_id}"

    def build_resources(self, providers, machine_types, env=None):
        """Build the full tc-admin resources to compare and build the pool"""

        # Select a cloud provider according to configuration
        assert self.cloud in providers, f"Cloud Provider {self.cloud} not available"
        provider = providers[self.cloud]

        pools = list(self.iterpools())
        all_scopes = tuple(
            set(chain.from_iterable(pool.get_scopes() for pool in pools))
        )

        # Build the pool configuration for selected machines
        machines = self.get_machine_list(machine_types)
        config = {
            "launchConfigs": provider.build_launch_configs(
                self.imageset, machines, self.disk_size, self.platform
            ),
            "maxCapacity": max(sum(pool.tasks for pool in pools) * 2, 3),
            "minCapacity": 0,
        }
        if self.platform == "linux":
            config["lifecycle"] = {
                # give workers 15 minutes to register before assuming they're broken
                "registrationTimeout": parse_time("15m"),
                "reregistrationTimeout": parse_time("4d"),
            }

        # Build the decision task payload that will trigger the new fuzzing tasks
        decision_task = yaml.safe_load(
            DECISION_TASK.substitute(
                description=DESCRIPTION.replace("\n", "\\n"),
                max_run_time=parse_time("1h"),
                owner_email=OWNER_EMAIL,
                pool_id=self.pool_id,
                provisioner=PROVISIONER_ID,
                scheduler=SCHEDULER_ID,
                secret=DECISION_TASK_SECRET,
                task_id=self.task_id,
            )
        )
        decision_task["scopes"] = sorted(chain(decision_task["scopes"], all_scopes))
        add_capabilities_for_scopes(decision_task)
        if env is not None:
            assert set(decision_task["payload"]["env"]).isdisjoint(set(env))
            decision_task["payload"]["env"].update(env)

        if self.cloud != "static":
            yield WorkerPool(
                config=config,
                description=DESCRIPTION.replace("\n", "\\n"),
                emailOnError=True,
                owner=OWNER_EMAIL,
                providerId=PROVIDER_IDS[self.cloud],
                workerPoolId=f"{WORKER_POOL_PREFIX}/{self.task_id}",
            )

        yield Hook(
            bindings=(),
            description=DESCRIPTION,
            emailOnError=True,
            hookGroupId=HOOK_PREFIX,
            hookId=self.task_id,
            name=self.task_id,
            owner=OWNER_EMAIL,
            schedule=list(self.cycle_crons()),
            task=decision_task,
            triggerSchema={},
        )

        yield Role(
            roleId=f"hook-id:{HOOK_PREFIX}/{self.task_id}",
            description=DESCRIPTION,
            scopes=decision_task["scopes"]
            + ["queue:create-task:highest:proj-fuzzing/ci"],
        )

    def build_tasks(self, parent_task_id, env=None):
        """Create fuzzing tasks and attach them to a decision task"""
        now = datetime.utcnow()

        for pool in self.iterpools():
            for i in range(1, pool.tasks + 1):
                task = yaml.safe_load(
                    FUZZING_TASK.substitute(
                        created=stringDate(now),
                        deadline=stringDate(now + timedelta(seconds=pool.max_run_time)),
                        description=DESCRIPTION.replace("\n", "\\n"),
                        expires=stringDate(fromNow("4 weeks", now)),
                        max_run_time=pool.max_run_time,
                        name=(
                            f"Fuzzing task {pool.platform}-{pool.pool_id} - "
                            f"{i}/{pool.tasks}"
                        ),
                        owner_email=OWNER_EMAIL,
                        pool_id=pool.pool_id,
                        provisioner=PROVISIONER_ID,
                        scheduler=SCHEDULER_ID,
                        secret=DECISION_TASK_SECRET,
                        task_group=parent_task_id,
                        task_id=self.task_id,
                    )
                )
                configure_task(task, pool, now, env)
                yield slugId(), task


class PoolConfigLoader:
    @staticmethod
    def from_file(pool_yml):
        assert pool_yml.is_file()
        data = yaml.safe_load(pool_yml.read_text())
        for cls in (PoolConfiguration, PoolConfigMap):
            if set(cls.FIELD_TYPES) >= set(data) >= cls.REQUIRED_FIELDS:
                return cls(pool_yml.stem, data, base_dir=pool_yml.parent)
        LOG.error(
            f"{pool_yml} has keys {data.keys()} and expected all of either "
            f"{PoolConfiguration.REQUIRED_FIELDS} or {PoolConfigMap.REQUIRED_FIELDS} "
            f"to exist."
        )
        raise RuntimeError(f"{pool_yml} type could not be identified!")
