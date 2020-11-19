# coding: utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Decision module for Orion builds"""

from datetime import timedelta
import os

from dateutil.relativedelta import relativedelta
from taskcluster.helper import TaskclusterConfig


Taskcluster = TaskclusterConfig(
    os.getenv("TASKCLUSTER_ROOT_URL", "https://community-tc.services.mozilla.com")
)
ARTIFACTS_EXPIRE = relativedelta(months=6)  # timedelta doesn't support months
DEADLINE = timedelta(hours=2)
MAX_RUN_TIME = timedelta(hours=1)
OWNER_EMAIL = "truber@mozilla.com"
PROVISIONER_ID = "proj-fuzzing"
SCHEDULER_ID = "taskcluster-github"
SOURCE_URL = "https://github.com/MozillaSecurity/orion"
WORKER_TYPE = "ci"
del os, relativedelta, timedelta, TaskclusterConfig