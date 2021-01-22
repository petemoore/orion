# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.

import itertools
import logging
import math
import os
from datetime import datetime, timedelta

import yaml
from taskcluster.exceptions import TaskclusterFailure, TaskclusterRestFailure
from taskcluster.utils import fromNow, slugId, stringDate
from tcadmin.resources import Hook, Role, WorkerPool

from ..common import taskcluster
from ..common.pool import PoolConfigMap as CommonPoolConfigMap
from ..common.pool import PoolConfiguration as CommonPoolConfiguration
from ..common.pool import parse_time
from . import (
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

Fuzzing workers generated by decision task"""

DOCKER_WORKER_DEVICES = (
    "cpu",
    "hostSharedMemory",
    "loopbackAudio",
    "loopbackVideo",
    "kvm",
)


def add_capabilities_for_scopes(task):
    """Request capabilities to match the scopes specified by the task"""
    capabilities = task["payload"]["capabilities"]
    scopes = set(task["scopes"])
    capabilities.setdefault("devices", {})
    for device in DOCKER_WORKER_DEVICES:
        if f"docker-worker:capability:device:{device}" in scopes:
            capabilities["devices"][device] = True
    if "docker-worker:capability:privileged" in scopes:
        capabilities["privileged"] = True
    if not capabilities["devices"]:
        del capabilities["devices"]


def cancel_tasks(worker_type):
    # Avoid cancelling self
    self_task_id = os.getenv("TASK_ID")

    hooks = taskcluster.get_service("hooks")
    queue = taskcluster.get_service("queue")

    # Get tasks by hook
    def iter_tasks_by_hook(hook_id):
        try:
            for fire in hooks.listLastFires(HOOK_PREFIX, hook_id)["lastFires"]:
                if fire["result"] != "success":
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

    def build_resources(self, providers, machine_types, env=None):
        """Build the full tc-admin resources to compare and build the pool"""

        # Select a cloud provider according to configuration
        assert self.cloud in providers, f"Cloud Provider {self.cloud} not available"
        provider = providers[self.cloud]

        # Build the pool configuration for selected machines
        machines = self.get_machine_list(machine_types)
        config = {
            "minCapacity": 0,
            "maxCapacity": (
                # add +1 to expected size, so if we manually trigger the hook, the new
                # decision can run without also manually cancelling a task
                # * 2 since Taskcluster seems to not reuse workers very quickly in some
                # cases, so we end up with a lot of pending tasks.
                max(1, math.ceil(self.max_run_time / self.cycle_time)) * self.tasks * 2
                + 1
            ),
            "launchConfigs": provider.build_launch_configs(
                self.imageset, machines, self.disk_size
            ),
            "lifecycle": {
                # give workers 15 minutes to register before assuming they're broken
                "registrationTimeout": parse_time("15m"),
                "reregistrationTimeout": parse_time("12h"),
            },
        }

        # Mandatory scopes to execute the hook
        # or create new tasks
        decision_task_scopes = (
            f"queue:scheduler-id:{SCHEDULER_ID}",
            f"queue:cancel-task:{SCHEDULER_ID}/*",
            f"queue:create-task:highest:{PROVISIONER_ID}/{self.task_id}",
            f"secrets:get:{DECISION_TASK_SECRET}",
        )

        # Build the decision task payload that will trigger the new fuzzing tasks
        decision_task = {
            "created": {"$fromNow": "0 seconds"},
            "deadline": {"$fromNow": "1 hour"},
            "expires": {"$fromNow": "1 week"},
            "extra": {},
            "metadata": {
                "description": DESCRIPTION,
                "name": f"Fuzzing decision {self.task_id}",
                "owner": OWNER_EMAIL,
                "source": "https://github.com/MozillaSecurity/orion",
            },
            "payload": {
                "artifacts": {},
                "cache": {},
                "capabilities": {},
                "env": {"TASKCLUSTER_SECRET": DECISION_TASK_SECRET},
                "features": {"taskclusterProxy": True},
                # this will always be run from a fuzzing-decision
                # task, so we could copy image from this TASK's definition
                "image": {
                    "type": "indexed-image",
                    "path": "public/fuzzing-decision.tar.zst",
                    "namespace": "project.fuzzing.orion.fuzzing-decision.master",
                },
                "command": ["fuzzing-decision", self.pool_id],
                "maxRunTime": parse_time("1h"),
            },
            "priority": "high",
            "provisionerId": PROVISIONER_ID,
            "workerType": self.task_id,
            "retries": 5,
            "routes": [],
            "schedulerId": SCHEDULER_ID,
            "scopes": tuple(self.scopes) + decision_task_scopes,
            "tags": {},
        }
        add_capabilities_for_scopes(decision_task)
        if env is not None:
            assert set(decision_task["payload"]["env"].keys()).isdisjoint(
                set(env.keys())
            )
            decision_task["payload"]["env"].update(env)

        pool = WorkerPool(
            workerPoolId=f"{WORKER_POOL_PREFIX}/{self.task_id}",
            providerId=PROVIDER_IDS[self.cloud],
            description=DESCRIPTION,
            owner=OWNER_EMAIL,
            emailOnError=True,
            config=config,
        )

        hook = Hook(
            hookGroupId=HOOK_PREFIX,
            hookId=self.task_id,
            name=self.task_id,
            description="Generated Fuzzing hook",
            owner=OWNER_EMAIL,
            emailOnError=True,
            schedule=list(self.cycle_crons()),
            task=decision_task,
            bindings=(),
            triggerSchema={},
        )

        role = Role(
            roleId=f"hook-id:{HOOK_PREFIX}/{self.task_id}",
            description=DESCRIPTION,
            scopes=tuple(self.scopes) + decision_task_scopes,
        )

        return [pool, hook, role]

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
            "path": "/logs/",
            "type": "directory",
        }
        return result

    def build_tasks(self, parent_task_id, env=None):
        """Create fuzzing tasks and attach them to a decision task"""
        now = datetime.utcnow()
        deps = [parent_task_id]

        preprocess = self.create_preprocess()
        if preprocess is not None:
            task_id = slugId()
            task = {
                "taskGroupId": parent_task_id,
                "dependencies": [parent_task_id],
                "created": stringDate(now),
                "deadline": stringDate(
                    now + timedelta(seconds=preprocess.max_run_time)
                ),
                "expires": stringDate(fromNow("1 week", now)),
                "extra": {},
                "metadata": {
                    "description": DESCRIPTION,
                    "name": f"Fuzzing task {self.task_id} - preprocess",
                    "owner": OWNER_EMAIL,
                    "source": "https://github.com/MozillaSecurity/orion",
                },
                "payload": {
                    "artifacts": preprocess.artifact_map(
                        stringDate(fromNow("1 week", now))
                    ),
                    "cache": {},
                    "capabilities": {},
                    "env": {
                        "TASKCLUSTER_FUZZING_POOL": self.pool_id,
                        "TASKCLUSTER_SECRET": DECISION_TASK_SECRET,
                        "TASKCLUSTER_FUZZING_PREPROCESS": "1",
                    },
                    "features": {"taskclusterProxy": True},
                    "image": preprocess.container,
                    "maxRunTime": preprocess.max_run_time,
                },
                "priority": "high",
                "provisionerId": PROVISIONER_ID,
                "workerType": self.task_id,
                "retries": 5,
                "routes": [],
                "schedulerId": SCHEDULER_ID,
                "scopes": preprocess.scopes + [f"secrets:get:{DECISION_TASK_SECRET}"],
                "tags": {},
            }
            add_capabilities_for_scopes(task)
            if env is not None:
                assert set(task["payload"]["env"]).isdisjoint(set(env))
                task["payload"]["env"].update(env)
            deps.append(task_id)

            yield task_id, task

        for i in range(1, self.tasks + 1):
            task_id = slugId()
            task = {
                "taskGroupId": parent_task_id,
                "dependencies": deps,
                "created": stringDate(now),
                "deadline": stringDate(now + timedelta(seconds=self.max_run_time)),
                "expires": stringDate(fromNow("1 week", now)),
                "extra": {},
                "metadata": {
                    "description": DESCRIPTION,
                    "name": f"Fuzzing task {self.task_id} - {i}/{self.tasks}",
                    "owner": OWNER_EMAIL,
                    "source": "https://github.com/MozillaSecurity/orion",
                },
                "payload": {
                    "artifacts": self.artifact_map(stringDate(fromNow("1 week", now))),
                    "cache": {},
                    "capabilities": {},
                    "env": {
                        "TASKCLUSTER_FUZZING_POOL": self.pool_id,
                        "TASKCLUSTER_SECRET": DECISION_TASK_SECRET,
                    },
                    "features": {"taskclusterProxy": True},
                    "image": self.container,
                    "maxRunTime": self.max_run_time,
                },
                "priority": "high",
                "provisionerId": PROVISIONER_ID,
                "workerType": self.task_id,
                "retries": 5,
                "routes": [],
                "schedulerId": SCHEDULER_ID,
                "scopes": self.scopes + [f"secrets:get:{DECISION_TASK_SECRET}"],
                "tags": {},
            }
            add_capabilities_for_scopes(task)
            if env is not None:
                assert set(task["payload"]["env"]).isdisjoint(set(env))
                task["payload"]["env"].update(env)

            yield task_id, task


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
            set(itertools.chain.from_iterable(pool.scopes for pool in pools))
        )

        # Build the pool configuration for selected machines
        machines = self.get_machine_list(machine_types)
        config = {
            "minCapacity": 0,
            "maxCapacity": max(sum(pool.tasks for pool in pools) * 2, 3),
            "launchConfigs": provider.build_launch_configs(
                self.imageset, machines, self.disk_size
            ),
            "lifecycle": {
                # give workers 15 minutes to register before assuming they're broken
                "registrationTimeout": parse_time("15m"),
                "reregistrationTimeout": parse_time("12h"),
            },
        }

        # Mandatory scopes to execute the hook
        # or create new tasks
        decision_task_scopes = (
            f"queue:scheduler-id:{SCHEDULER_ID}",
            f"queue:create-task:highest:{PROVISIONER_ID}/{self.task_id}",
            f"secrets:get:{DECISION_TASK_SECRET}",
        )

        # Build the decision task payload that will trigger the new fuzzing tasks
        decision_task = {
            "created": {"$fromNow": "0 seconds"},
            "deadline": {"$fromNow": "1 hour"},
            "expires": {"$fromNow": "1 week"},
            "extra": {},
            "metadata": {
                "description": DESCRIPTION,
                "name": f"Fuzzing decision {self.task_id}",
                "owner": OWNER_EMAIL,
                "source": "https://github.com/MozillaSecurity/orion",
            },
            "payload": {
                "artifacts": {},
                "cache": {},
                "capabilities": {},
                "env": {"TASKCLUSTER_SECRET": DECISION_TASK_SECRET},
                "features": {"taskclusterProxy": True},
                # this will always be run from a fuzzing-decision
                # task, so we could copy image from this TASK's definition
                "image": {
                    "type": "indexed-image",
                    "path": "public/fuzzing-decision.tar.zst",
                    "namespace": "project.fuzzing.orion.fuzzing-decision.master",
                },
                "command": ["fuzzing-decision", self.pool_id],
                "maxRunTime": parse_time("1h"),
            },
            "priority": "high",
            "provisionerId": PROVISIONER_ID,
            "workerType": self.task_id,
            "retries": 5,
            "routes": [],
            "schedulerId": SCHEDULER_ID,
            "scopes": all_scopes + decision_task_scopes,
            "tags": {},
        }
        add_capabilities_for_scopes(decision_task)
        if env is not None:
            assert set(decision_task["payload"]["env"].keys()).isdisjoint(
                set(env.keys())
            )
            decision_task["payload"]["env"].update(env)

        pool = WorkerPool(
            workerPoolId=f"{WORKER_POOL_PREFIX}/{self.task_id}",
            providerId=PROVIDER_IDS[self.cloud],
            description=DESCRIPTION,
            owner=OWNER_EMAIL,
            emailOnError=True,
            config=config,
        )

        hook = Hook(
            hookGroupId=HOOK_PREFIX,
            hookId=self.task_id,
            name=self.task_id,
            description="Generated Fuzzing hook",
            owner=OWNER_EMAIL,
            emailOnError=True,
            schedule=list(self.cycle_crons()),
            task=decision_task,
            bindings=(),
            triggerSchema={},
        )

        role = Role(
            roleId=f"hook-id:{HOOK_PREFIX}/{self.task_id}",
            description=DESCRIPTION,
            scopes=all_scopes + decision_task_scopes,
        )

        return [pool, hook, role]

    def build_tasks(self, parent_task_id, env=None):
        """Create fuzzing tasks and attach them to a decision task"""
        now = datetime.utcnow()
        deps = [parent_task_id]

        for pool in self.iterpools():
            for i in range(1, pool.tasks + 1):
                task_id = slugId()
                task = {
                    "taskGroupId": parent_task_id,
                    "dependencies": deps,
                    "created": stringDate(now),
                    "deadline": stringDate(now + timedelta(seconds=pool.max_run_time)),
                    "expires": stringDate(fromNow("1 week", now)),
                    "extra": {},
                    "metadata": {
                        "description": DESCRIPTION,
                        "name": (
                            f"Fuzzing task {pool.platform}-{pool.pool_id} - "
                            f"{i}/{pool.tasks}"
                        ),
                        "owner": OWNER_EMAIL,
                        "source": "https://github.com/MozillaSecurity/orion",
                    },
                    "payload": {
                        "artifacts": {
                            "project/fuzzing/private/logs": {
                                "expires": stringDate(fromNow("1 week", now)),
                                "path": "/logs/",
                                "type": "directory",
                            }
                        },
                        "cache": {},
                        "capabilities": {},
                        "env": {
                            "TASKCLUSTER_FUZZING_POOL": pool.pool_id,
                            "TASKCLUSTER_SECRET": DECISION_TASK_SECRET,
                        },
                        "features": {"taskclusterProxy": True},
                        "image": pool.container,
                        "maxRunTime": pool.max_run_time,
                    },
                    "priority": "high",
                    "provisionerId": PROVISIONER_ID,
                    "workerType": self.task_id,
                    "retries": 5,
                    "routes": [],
                    "schedulerId": SCHEDULER_ID,
                    "scopes": pool.scopes + [f"secrets:get:{DECISION_TASK_SECRET}"],
                    "tags": {},
                }
                add_capabilities_for_scopes(task)
                if env is not None:
                    assert set(task["payload"]["env"]).isdisjoint(set(env))
                    task["payload"]["env"].update(env)

                yield task_id, task


class PoolConfigLoader:
    @staticmethod
    def from_file(pool_yml):
        assert pool_yml.is_file()
        data = yaml.safe_load(pool_yml.read_text())
        for cls in (PoolConfiguration, PoolConfigMap):
            if set(cls.FIELD_TYPES) >= set(data.keys()) >= cls.REQUIRED_FIELDS:
                return cls(pool_yml.stem, data, base_dir=pool_yml.parent)
        LOG.error(
            f"{pool_yml} has keys {data.keys()} and expected all of either "
            f"{PoolConfiguration.REQUIRED_FIELDS} or {PoolConfigMap.REQUIRED_FIELDS} "
            f"to exist."
        )
        raise RuntimeError(f"{pool_yml} type could not be identified!")
