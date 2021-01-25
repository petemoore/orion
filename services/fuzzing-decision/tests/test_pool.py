# -*- coding: utf-8 -*-

import copy
import datetime
from pathlib import Path

import pytest
import slugid

from fuzzing_decision.common.pool import PoolConfigLoader as CommonPoolConfigLoader
from fuzzing_decision.common.pool import PoolConfigMap as CommonPoolConfigMap
from fuzzing_decision.common.pool import PoolConfiguration as CommonPoolConfiguration
from fuzzing_decision.common.pool import parse_size
from fuzzing_decision.decision.pool import (
    DOCKER_WORKER_DEVICES,
    PoolConfigLoader,
    PoolConfigMap,
    PoolConfiguration,
)

POOL_FIXTURES = Path(__file__).parent / "fixtures" / "pools"

WST_URL = "https://community-websocktunnel.services.mozilla.com"


@pytest.mark.parametrize(
    "size, divisor, result",
    [
        ("2g", "1g", 2),
        ("2g", "1m", 2048),
        ("2g", 1, 2048 * 1024 * 1024),
        ("128t", "1g", 128 * 1024),
    ],
)
def test_parse_size(size, divisor, result):
    if isinstance(divisor, str):
        divisor = parse_size(divisor)

    assert parse_size(size) / divisor == result


@pytest.mark.parametrize(
    "provider, cpu, cores, ram, metal, result",
    [
        ("gcp", "x64", 1, 1, False, ["base", "metal"]),
        ("gcp", "x64", 2, 1, False, ["2-cpus", "more-ram"]),
        ("gcp", "x64", 2, 5, False, ["more-ram"]),
        ("gcp", "x64", 1, 1, True, ["metal"]),
        ("aws", "arm64", 1, 1, False, ["a1"]),
        ("aws", "arm64", 2, 1, False, ["a2"]),
        ("aws", "arm64", 12, 32, False, []),
        ("aws", "arm64", 1, 1, True, []),
        # x64 is not present in aws
        ("aws", "x64", 1, 1, False, KeyError),
        # invalid provider raises too
        ("dummy", "x64", 1, 1, False, KeyError),
    ],
)
def test_machine_filters(mock_machines, provider, cpu, ram, cores, metal, result):

    if isinstance(result, list):
        assert list(mock_machines.filter(provider, cpu, cores, ram, metal)) == result
    else:
        with pytest.raises(result):
            list(mock_machines.filter(provider, cpu, cores, ram, metal))


# Hook & role should be the same across cloud providers
VALID_HOOK = {
    "kind": "Hook",
    "bindings": [],
    "emailOnError": True,
    "hookGroupId": "project-fuzzing",
    "hookId": "linux-test",
    "name": "linux-test",
    "owner": "fuzzing+taskcluster@mozilla.com",
    "schedule": ["0 0 12 * * *", "0 0 0 * * *"],
    "task": {
        "created": {"$fromNow": "0 seconds"},
        "deadline": {"$fromNow": "1 hour"},
        "expires": {"$fromNow": "1 week"},
        "extra": {},
        "metadata": {
            "description": "*DO NOT EDIT* - This resource is "
            "configured automatically.\n"
            "\n"
            "Fuzzing workers generated by decision "
            "task",
            "name": "Fuzzing decision linux-test",
            "owner": "fuzzing+taskcluster@mozilla.com",
            "source": "https://github.com/MozillaSecurity/orion",
        },
        "payload": {
            "artifacts": {},
            "cache": {},
            "capabilities": {},
            "env": {"TASKCLUSTER_SECRET": "project/fuzzing/decision"},
            "features": {"taskclusterProxy": True},
            "image": {
                "namespace": "project.fuzzing.orion.fuzzing-decision.master",
                "path": "public/fuzzing-decision.tar.zst",
                "type": "indexed-image",
            },
            "command": ["fuzzing-decision", "test"],
            "maxRunTime": 3600,
        },
        "priority": "high",
        "provisionerId": "proj-fuzzing",
        "retries": 5,
        "routes": [],
        "schedulerId": "-",
        "scopes": [
            "queue:scheduler-id:-",
            "queue:cancel-task:-/*",
            "queue:create-task:highest:proj-fuzzing/linux-test",
            "secrets:get:project/fuzzing/decision",
        ],
        "tags": {},
        "workerType": "linux-test",
    },
    "triggerSchema": {},
    "description": "*DO NOT EDIT* - This resource is configured automatically.\n"
    "\n"
    "Generated Fuzzing hook",
}

VALID_ROLE = {
    "kind": "Role",
    "roleId": "hook-id:project-fuzzing/linux-test",
    "scopes": [
        "queue:cancel-task:-/*",
        "queue:create-task:highest:proj-fuzzing/linux-test",
        "queue:scheduler-id:-",
        "secrets:get:project/fuzzing/decision",
    ],
    "description": "*DO NOT EDIT* - This resource is configured automatically.\n"
    "\n"
    "Fuzzing workers generated by decision task",
}


@pytest.mark.parametrize("env", [(None), ({"someKey": "someValue"})])
def test_aws_resources(env, mock_clouds, mock_machines):

    conf = PoolConfiguration(
        "test",
        {
            "cloud": "aws",
            "scopes": [],
            "disk_size": "120g",
            "schedule_start": "1970-01-01T00:00:00Z",
            "cycle_time": "12h",
            "max_run_time": "12h",
            "cores_per_task": 2,
            "metal": False,
            "name": "Amazing fuzzing pool",
            "tasks": 3,
            "command": ["run-fuzzing.sh"],
            "container": "MozillaSecurity/fuzzer:latest",
            "minimum_memory_per_core": "1g",
            "imageset": "generic-worker-A",
            "parents": [],
            "cpu": "arm64",
            "platform": "linux",
            "preprocess": None,
            "macros": {},
        },
    )
    resources = conf.build_resources(mock_clouds, mock_machines, env=env)
    assert len(resources) == 3
    pool, hook, role = resources

    assert pool.to_json() == {
        "kind": "WorkerPool",
        "config": {
            "launchConfigs": [
                {
                    "capacityPerInstance": 1,
                    "launchConfig": {
                        "ImageId": "ami-1234",
                        "InstanceMarketOptions": {"MarketType": "spot"},
                        "InstanceType": "a2",
                        "Placement": {"AvailabilityZone": "us-west-1a"},
                        "SecurityGroupIds": ["sg-A"],
                        "SubnetId": "subnet-XXX",
                    },
                    "region": "us-west-1",
                    "workerConfig": {
                        "dockerConfig": {
                            "allowPrivileged": True,
                            "allowDisableSeccomp": True,
                        },
                        "genericWorker": {
                            "config": {
                                "anyKey": "anyValue",
                                "deploymentId": "d9465de45bdee4be",
                                "os": "linux",
                                "wstAudience": "communitytc",
                                "wstServerURL": WST_URL,
                            },
                        },
                        "shutdown": {"afterIdleSeconds": 15, "enabled": True},
                    },
                }
            ],
            "maxCapacity": 7,
            "minCapacity": 0,
            "lifecycle": {"registrationTimeout": 900, "reregistrationTimeout": 43200},
        },
        "emailOnError": True,
        "owner": "fuzzing+taskcluster@mozilla.com",
        "providerId": "community-tc-workers-aws",
        "workerPoolId": "proj-fuzzing/linux-test",
        "description": "*DO NOT EDIT* - This resource is configured automatically.\n"
        "\n"
        "Fuzzing workers generated by decision task",
    }

    # Update env in valid hook
    valid_hook = copy.deepcopy(VALID_HOOK)
    if env is not None:
        valid_hook["task"]["payload"]["env"].update(env)
    assert hook.to_json() == valid_hook
    assert role.to_json() == VALID_ROLE


@pytest.mark.parametrize("env", [(None), ({"someKey": "someValue"})])
def test_gcp_resources(env, mock_clouds, mock_machines):

    conf = PoolConfiguration(
        "test",
        {
            "cloud": "gcp",
            "scopes": [],
            "disk_size": "120g",
            "schedule_start": "1970-01-01T00:00:00Z",
            "cycle_time": "12h",
            "max_run_time": "12h",
            "cores_per_task": 2,
            "metal": False,
            "name": "Amazing fuzzing pool",
            "tasks": 3,
            "command": ["run-fuzzing.sh"],
            "container": "MozillaSecurity/fuzzer:latest",
            "minimum_memory_per_core": "1g",
            "imageset": "docker-worker",
            "parents": [],
            "cpu": "x64",
            "platform": "linux",
            "preprocess": None,
            "macros": {},
        },
    )
    resources = conf.build_resources(mock_clouds, mock_machines, env=env)
    assert len(resources) == 3
    pool, hook, role = resources

    assert pool.to_json() == {
        "kind": "WorkerPool",
        "config": {
            "launchConfigs": [
                {
                    "capacityPerInstance": 1,
                    "disks": [
                        {
                            "autoDelete": True,
                            "boot": True,
                            "initializeParams": {
                                "diskSizeGb": 120,
                                "sourceImage": "path/to/image",
                            },
                            "type": "PERSISTENT",
                        }
                    ],
                    "machineType": "zones/us-west1-a/machineTypes/2-cpus",
                    "networkInterfaces": [
                        {"accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]}
                    ],
                    "region": "us-west1",
                    "scheduling": {"onHostMaintenance": "terminate"},
                    "workerConfig": {
                        "dockerConfig": {
                            "allowPrivileged": True,
                            "allowDisableSeccomp": True,
                        },
                        "genericWorker": {
                            "config": {
                                "deploymentId": "ff173b0660475975",
                                "wstAudience": "communitytc",
                                "wstServerURL": WST_URL,
                            },
                        },
                        "shutdown": {"afterIdleSeconds": 15, "enabled": True},
                    },
                    "zone": "us-west1-a",
                },
                {
                    "capacityPerInstance": 1,
                    "disks": [
                        {
                            "autoDelete": True,
                            "boot": True,
                            "initializeParams": {
                                "diskSizeGb": 120,
                                "sourceImage": "path/to/image",
                            },
                            "type": "PERSISTENT",
                        }
                    ],
                    "machineType": "zones/us-west1-b/machineTypes/2-cpus",
                    "networkInterfaces": [
                        {"accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]}
                    ],
                    "region": "us-west1",
                    "scheduling": {"onHostMaintenance": "terminate"},
                    "workerConfig": {
                        "dockerConfig": {
                            "allowPrivileged": True,
                            "allowDisableSeccomp": True,
                        },
                        "genericWorker": {
                            "config": {
                                "deploymentId": "ff173b0660475975",
                                "wstAudience": "communitytc",
                                "wstServerURL": WST_URL,
                            },
                        },
                        "shutdown": {"afterIdleSeconds": 15, "enabled": True},
                    },
                    "zone": "us-west1-b",
                },
                {
                    "capacityPerInstance": 1,
                    "disks": [
                        {
                            "autoDelete": True,
                            "boot": True,
                            "initializeParams": {
                                "diskSizeGb": 120,
                                "sourceImage": "path/to/image",
                            },
                            "type": "PERSISTENT",
                        }
                    ],
                    "machineType": "zones/us-west1-a/machineTypes/more-ram",
                    "networkInterfaces": [
                        {"accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]}
                    ],
                    "region": "us-west1",
                    "scheduling": {"onHostMaintenance": "terminate"},
                    "workerConfig": {
                        "dockerConfig": {
                            "allowPrivileged": True,
                            "allowDisableSeccomp": True,
                        },
                        "genericWorker": {
                            "config": {
                                "deploymentId": "ff173b0660475975",
                                "wstAudience": "communitytc",
                                "wstServerURL": WST_URL,
                            },
                        },
                        "shutdown": {"afterIdleSeconds": 15, "enabled": True},
                    },
                    "zone": "us-west1-a",
                },
            ],
            "maxCapacity": 7,
            "minCapacity": 0,
            "lifecycle": {"registrationTimeout": 900, "reregistrationTimeout": 43200},
        },
        "emailOnError": True,
        "owner": "fuzzing+taskcluster@mozilla.com",
        "providerId": "community-tc-workers-google",
        "workerPoolId": "proj-fuzzing/linux-test",
        "description": "*DO NOT EDIT* - This resource is configured automatically.\n"
        "\n"
        "Fuzzing workers generated by decision task",
    }

    # Update env in valid hook
    valid_hook = copy.deepcopy(VALID_HOOK)
    if env is not None:
        valid_hook["task"]["payload"]["env"].update(env)
    assert hook.to_json() == valid_hook
    assert role.to_json() == VALID_ROLE


@pytest.mark.parametrize("env", [None, {"someKey": "someValue"}])
@pytest.mark.parametrize(
    "scope_caps",
    [
        ([], {}),
        (["docker-worker:capability:privileged"], {"privileged": True}),
        (
            ["docker-worker:capability:privileged"]
            + [
                f"docker-worker:capability:device:{dev}"
                for dev in DOCKER_WORKER_DEVICES
            ],
            {
                "privileged": True,
                "devices": {dev: True for dev in DOCKER_WORKER_DEVICES},
            },
        ),
    ],
)
def test_tasks(env, scope_caps):
    scopes, expected_capabilities = scope_caps
    conf = PoolConfiguration(
        "test",
        {
            "cloud": "gcp",
            "scopes": scopes,
            "disk_size": "10g",
            "cycle_time": "1h",
            "max_run_time": "30s",
            "schedule_start": None,
            "cores_per_task": 1,
            "metal": False,
            "name": "Amazing fuzzing pool",
            "tasks": 2,
            "command": ["run-fuzzing.sh"],
            "container": "MozillaSecurity/fuzzer:latest",
            "minimum_memory_per_core": "1g",
            "imageset": "anything",
            "parents": [],
            "cpu": "x64",
            "platform": "linux",
            "preprocess": None,
            "macros": {},
            "artifacts": {
                "/some-file.txt": {
                    "type": "file",
                    "url": "project/fuzzing/private/file.txt",
                },
                "/var/log/": {
                    "type": "directory",
                    "url": "project/fuzzing/private/var-log",
                },
            },
        },
    )

    task_ids, tasks = zip(*conf.build_tasks("someTaskId", env=env))

    # Check we have 2 valid generated task ids
    assert len(task_ids) == 2
    assert all(map(slugid.decode, task_ids))

    # Check we have 2 valid task definitions
    assert len(tasks) == 2

    def _check_date(task, *keys):
        # Dates can not be checked directly as they are generated
        assert keys, "must specify at least one key"
        value = task
        for key in keys:
            obj = value
            assert isinstance(obj, dict)
            value = obj[key]
        assert isinstance(value, str)
        date = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        del obj[key]
        return date

    for i, task in enumerate(tasks):
        created = _check_date(task, "created")
        deadline = _check_date(task, "deadline")
        expires = _check_date(task, "expires")
        assert expires >= deadline > created
        expected_env = {
            "TASKCLUSTER_FUZZING_POOL": "test",
            "TASKCLUSTER_SECRET": "project/fuzzing/decision",
        }
        if env is not None:
            expected_env.update(env)

        log_expires = _check_date(
            task, "payload", "artifacts", "project/fuzzing/private/logs", "expires"
        )
        assert log_expires == expires
        log_expires = _check_date(
            task, "payload", "artifacts", "project/fuzzing/private/file.txt", "expires"
        )
        assert log_expires == expires
        log_expires = _check_date(
            task, "payload", "artifacts", "project/fuzzing/private/var-log", "expires"
        )
        assert log_expires == expires
        assert set(task["scopes"]) == set(
            ["secrets:get:project/fuzzing/decision"] + scopes
        )
        # scopes are already asserted above
        # - read the value for comparison instead of deleting the key, so the object is
        #   printed in full on failure
        scopes = task["scopes"]
        assert task == {
            "dependencies": ["someTaskId"],
            "extra": {},
            "metadata": {
                "description": "*DO NOT EDIT* - This resource is configured "
                "automatically.\n"
                "\n"
                "Fuzzing workers generated by decision task",
                "name": f"Fuzzing task linux-test - {i+1}/2",
                "owner": "fuzzing+taskcluster@mozilla.com",
                "source": "https://github.com/MozillaSecurity/orion",
            },
            "payload": {
                "artifacts": {
                    "project/fuzzing/private/logs": {
                        "path": "/logs/",
                        "type": "directory",
                    },
                    "project/fuzzing/private/file.txt": {
                        "path": "/some-file.txt",
                        "type": "file",
                    },
                    "project/fuzzing/private/var-log": {
                        "path": "/var/log/",
                        "type": "directory",
                    },
                },
                "cache": {},
                "capabilities": expected_capabilities,
                "env": expected_env,
                "features": {"taskclusterProxy": True},
                "image": "MozillaSecurity/fuzzer:latest",
                "maxRunTime": 30,
            },
            "priority": "high",
            "provisionerId": "proj-fuzzing",
            "retries": 5,
            "routes": [],
            "schedulerId": "-",
            "scopes": scopes,
            "tags": {},
            "taskGroupId": "someTaskId",
            "workerType": "linux-test",
        }


def test_preprocess_tasks():
    conf = PoolConfiguration.from_file(POOL_FIXTURES / "pre-pool.yml")

    task_ids, tasks = zip(*conf.build_tasks("someTaskId"))

    # Check we have 2 valid generated task ids
    assert len(task_ids) == 2
    assert all(map(slugid.decode, task_ids))

    # Check we have 2 valid task definitions
    assert len(tasks) == 2

    def _check_date(task, *keys):
        # Dates can not be checked directly as they are generated
        assert keys, "must specify at least one key"
        value = task
        for key in keys:
            obj = value
            assert isinstance(obj, dict)
            value = obj[key]
        assert isinstance(value, str)
        date = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        del obj[key]
        return date

    expected = [
        {
            "name": "preprocess",
            "deps": ["someTaskId"],
            "extra_env": {"TASKCLUSTER_FUZZING_PREPROCESS": "1"},
        },
        {"name": "1/1", "deps": ["someTaskId", task_ids[0]], "extra_env": {}},
    ]
    for task, expect in zip(tasks, expected):
        created = _check_date(task, "created")
        deadline = _check_date(task, "deadline")
        expires = _check_date(task, "expires")
        assert expires >= deadline > created
        expected_env = {
            "TASKCLUSTER_FUZZING_POOL": "pre-pool",
            "TASKCLUSTER_SECRET": "project/fuzzing/decision",
        }
        expected_env.update(expect["extra_env"])

        log_expires = _check_date(
            task, "payload", "artifacts", "project/fuzzing/private/logs", "expires"
        )
        assert log_expires == expires
        assert set(task["scopes"]) == set(["secrets:get:project/fuzzing/decision"])
        # scopes are already asserted above
        # - read the value for comparison instead of deleting the key, so the object is
        #   printed in full on failure
        scopes = task["scopes"]
        assert task == {
            "dependencies": expect["deps"],
            "extra": {},
            "metadata": {
                "description": "*DO NOT EDIT* - This resource is configured "
                "automatically.\n"
                "\n"
                "Fuzzing workers generated by decision task",
                "name": f"Fuzzing task linux-pre-pool - {expect['name']}",
                "owner": "fuzzing+taskcluster@mozilla.com",
                "source": "https://github.com/MozillaSecurity/orion",
            },
            "payload": {
                "artifacts": {
                    "project/fuzzing/private/logs": {
                        "path": "/logs/",
                        "type": "directory",
                    }
                },
                "cache": {},
                "capabilities": {},
                "env": expected_env,
                "features": {"taskclusterProxy": True},
                "image": "MozillaSecurity/fuzzer:latest",
                "maxRunTime": 3600,
            },
            "priority": "high",
            "provisionerId": "proj-fuzzing",
            "retries": 5,
            "routes": [],
            "schedulerId": "-",
            "scopes": scopes,
            "tags": {},
            "taskGroupId": "someTaskId",
            "workerType": "linux-pre-pool",
        }


@pytest.mark.parametrize("pool_path", POOL_FIXTURES.glob("pool*.yml"))
def test_flatten(pool_path):
    class PoolConfigNoFlatten(CommonPoolConfiguration):
        def _flatten(self, _):
            pass

    pool = CommonPoolConfiguration.from_file(pool_path)
    expect_path = pool_path.with_name(pool_path.name.replace("pool", "expect"))
    expect = PoolConfigNoFlatten.from_file(expect_path)
    assert pool.cloud == expect.cloud
    assert set(pool.scopes) == set(expect.scopes)
    assert pool.disk_size == expect.disk_size
    assert pool.cycle_time == expect.cycle_time
    assert pool.max_run_time == expect.max_run_time
    assert pool.schedule_start == expect.schedule_start
    assert pool.cores_per_task == expect.cores_per_task
    assert pool.metal == expect.metal
    assert pool.name == expect.name
    assert pool.tasks == expect.tasks
    assert pool.command == expect.command
    assert pool.container == expect.container
    assert pool.minimum_memory_per_core == expect.minimum_memory_per_core
    assert pool.imageset == expect.imageset
    assert pool.parents == expect.parents
    assert pool.cpu == expect.cpu
    assert pool.platform == expect.platform
    assert pool.preprocess == expect.preprocess
    assert pool.macros == expect.macros


def test_pool_map():
    class PoolConfigNoFlatten(CommonPoolConfiguration):
        def _flatten(self, _):
            pass

    cfg_map = CommonPoolConfigMap.from_file(POOL_FIXTURES / "map1.yml")
    expect = PoolConfigNoFlatten.from_file(POOL_FIXTURES / "expect1.yml")
    assert cfg_map.cloud == expect.cloud
    assert set(cfg_map.scopes) == set()
    assert cfg_map.disk_size == expect.disk_size
    assert cfg_map.cycle_time == expect.cycle_time
    assert cfg_map.max_run_time is None
    assert cfg_map.schedule_start == expect.schedule_start
    assert cfg_map.cores_per_task == expect.cores_per_task
    assert cfg_map.metal == expect.metal
    assert cfg_map.name == "mixed"
    assert cfg_map.tasks is None
    assert cfg_map.command is None
    assert cfg_map.container is None
    assert cfg_map.minimum_memory_per_core == expect.minimum_memory_per_core
    assert cfg_map.imageset == expect.imageset
    assert cfg_map.apply_to == ["pool1"]
    assert cfg_map.cpu == expect.cpu
    assert cfg_map.platform == expect.platform
    assert cfg_map.preprocess is None
    assert cfg_map.macros == {}

    pools = list(cfg_map.iterpools())
    assert len(pools) == 1
    pool = pools[0]
    assert pool.cloud == expect.cloud
    assert set(pool.scopes) == set(expect.scopes)
    assert pool.disk_size == expect.disk_size
    assert pool.cycle_time == expect.cycle_time
    assert pool.max_run_time == expect.max_run_time
    assert pool.schedule_start == expect.schedule_start
    assert pool.cores_per_task == expect.cores_per_task
    assert pool.metal == expect.metal
    assert pool.name == f"{expect.name} ({cfg_map.name})"
    assert pool.tasks == expect.tasks
    assert pool.command == expect.command
    assert pool.container == expect.container
    assert pool.minimum_memory_per_core == expect.minimum_memory_per_core
    assert pool.imageset == expect.imageset
    assert pool.parents == ["pool1"]
    assert pool.cpu == expect.cpu
    assert pool.platform == expect.platform
    assert pool.preprocess == expect.preprocess
    assert pool.macros == expect.macros


@pytest.mark.parametrize(
    "loader, config_cls, map_cls",
    [
        (CommonPoolConfigLoader, CommonPoolConfiguration, CommonPoolConfigMap),
        (PoolConfigLoader, PoolConfiguration, PoolConfigMap),
    ],
)
def test_pool_loader(loader, config_cls, map_cls):
    obj = loader.from_file(POOL_FIXTURES / "load-cfg.yml")
    assert isinstance(obj, config_cls)
    obj = loader.from_file(POOL_FIXTURES / "load-map.yml")
    assert isinstance(obj, map_cls)


def test_cycle_crons():
    conf = CommonPoolConfiguration(
        "test",
        {
            "schedule_start": "1970-01-01T00:00:00Z",
            "cloud": "gcp",
            "scopes": [],
            "disk_size": "10g",
            "cycle_time": "6h",
            "max_run_time": "6h",
            "cores_per_task": 1,
            "metal": False,
            "name": "Amazing fuzzing pool",
            "tasks": 2,
            "command": ["run-fuzzing.sh"],
            "container": "MozillaSecurity/fuzzer:latest",
            "minimum_memory_per_core": "1g",
            "imageset": "anything",
            "parents": [],
            "cpu": "x64",
            "platform": "linux",
            "preprocess": None,
            "macros": {},
        },
    )

    # cycle time 6h
    assert list(conf.cycle_crons()) == [
        "0 0 6 * * *",
        "0 0 12 * * *",
        "0 0 18 * * *",
        "0 0 0 * * *",
    ]

    # cycle time 3.5 days
    conf.cycle_time = 3600 * 24 * 3.5
    assert list(conf.cycle_crons()) == [
        "0 0 12 * * 0",
        "0 0 0 * * 4",
    ]

    # cycle time 17h
    conf.cycle_time = 3600 * 17
    crons = list(conf.cycle_crons())
    assert len(crons) == (365 * 24 // 17) + 1
    assert crons[:4] == ["0 0 17 1 1 *", "0 0 10 2 1 *", "0 0 3 3 1 *", "0 0 20 3 1 *"]

    # cycle time 48h
    conf.cycle_time = 3600 * 48
    crons = list(conf.cycle_crons())
    assert len(crons) == (365 * 24 // 48) + 1
    assert crons[:4] == ["0 0 0 3 1 *", "0 0 0 5 1 *", "0 0 0 7 1 *", "0 0 0 9 1 *"]

    # cycle time 72h
    conf.cycle_time = 3600 * 72
    crons = list(conf.cycle_crons())
    assert len(crons) == (365 * 24 // 72) + 1
    assert crons[:4] == ["0 0 0 4 1 *", "0 0 0 7 1 *", "0 0 0 10 1 *", "0 0 0 13 1 *"]

    # cycle time 17d
    conf.cycle_time = 3600 * 24 * 17
    crons = list(conf.cycle_crons())
    assert len(crons) == (365 // 17) + 1
    assert crons[:4] == ["0 0 0 18 1 *", "0 0 0 4 2 *", "0 0 0 21 2 *", "0 0 0 10 3 *"]

    # using schedule_start should be the same as using datetime.now()
    conf.schedule_start = None
    conf.cycle_time = 3600 * 12
    start = datetime.datetime.now(datetime.timezone.utc)
    calc_none = list(conf.cycle_crons())
    fin = datetime.datetime.now(datetime.timezone.utc)
    for offset in range(int((fin - start).total_seconds()) + 1):
        conf.schedule_start = start + datetime.timedelta(seconds=offset)
        if calc_none == list(conf.cycle_crons()):
            break
    else:
        assert calc_none == list(conf.cycle_crons())


def test_required():
    CommonPoolConfiguration("test", {"name": "test pool"}, _flattened={})
    with pytest.raises(AssertionError):
        CommonPoolConfiguration("test", {}, _flattened={})