# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Common definitions for Grizzly reduction in Taskcluster
"""


import argparse
import json
import re
from abc import ABC, abstractmethod
from argparse import ArgumentParser
from functools import wraps
from logging import DEBUG, INFO, WARNING, basicConfig, getLogger
from pathlib import Path
from typing import Any, Dict, List, Optional

from Reporter.Reporter import Reporter
from taskcluster.helper import TaskclusterConfig

# Shared taskcluster configuration
Taskcluster = TaskclusterConfig("https://community-tc.services.mozilla.com")
LOG = getLogger(__name__)


# this is duplicated from grizzly status_reporter.py
def format_seconds(duration: float) -> str:
    # format H:M:S, and then remove all leading zeros with regex
    minutes, seconds = divmod(int(duration), 60)
    hours, minutes = divmod(minutes, 60)
    result = re.sub("^[0:]*", "", "%d:%02d:%02d" % (hours, minutes, seconds))
    # if the result is all zeroes, ensure one zero is output
    if not result:
        result = "0"
    # a bare number is ambiguous. output 's' for seconds
    if ":" not in result:
        result += "s"
    return result


def remote_checks(wrapped):
    """Decorator to perform error checks before using remote features"""

    @wraps(wrapped)
    def decorator(self, *args: Any, **kwargs: Any):
        if not self.serverProtocol:
            raise RuntimeError(
                "Must specify serverProtocol (configuration property: serverproto) to "
                "use remote features."
            )
        if not self.serverPort:
            raise RuntimeError(
                "Must specify serverPort (configuration property: serverport) to use "
                "remote features."
            )
        if not self.serverHost:
            raise RuntimeError(
                "Must specify serverHost (configuration property: serverhost) to use "
                "remote features."
            )
        if not self.serverAuthToken:
            raise RuntimeError(
                "Must specify serverAuthToken (configuration property: "
                "serverauthtoken) to use remote features."
            )
        return wrapped(self, *args, **kwargs)

    return decorator


class CommonArgParser(ArgumentParser):
    """Argument parser with common arguments used by reduction scripts."""

    def __init__(self, *args: Any, **kwds: Any) -> None:
        super().__init__(*args, **kwds)
        group = self.add_mutually_exclusive_group()
        group.add_argument(
            "--quiet",
            "-q",
            dest="log_level",
            action="store_const",
            const=WARNING,
            help="Be less verbose",
        )
        group.add_argument(
            "--verbose",
            "-v",
            dest="log_level",
            action="store_const",
            const=DEBUG,
            help="Be more verbose",
        )
        self.set_defaults(log_level=INFO)


class CrashManager(Reporter):
    """Class to manage access to CrashManager server."""

    @remote_checks
    def _list_objs(
        self,
        endpoint: str,
        query: Optional[Dict[str, Any]] = None,
        ordering: Optional[List[str]] = None,
    ):
        """Iterate over results possibly paginated by Django Rest Framework."""
        params: Optional[Dict[str, Any]] = {}
        if query is not None:
            assert params is not None
            params["query"] = json.dumps(query)
        if ordering is not None:
            assert params is not None
            params["ordering"] = ",".join(ordering)
        if endpoint == "crashes":
            assert params is not None
            params["include_raw"] = "0"
        assert params is not None
        params["limit"] = 1000

        returned = 0

        next_url: Optional[str] = (
            f"{self.serverProtocol}://{self.serverHost}:{self.serverPort}"
            f"/crashmanager/rest/{endpoint}/"
        )

        while next_url:

            resp_json = self.get(next_url, params=params).json()

            if isinstance(resp_json, dict):
                next_url = resp_json["next"]
                results = resp_json["results"]
                total = resp_json["count"]
            elif isinstance(resp_json, list):
                next_url = None
                results = resp_json
                total = len(results)
            else:
                raise RuntimeError(
                    f"Server sent malformed JSON response: {resp_json!r}"
                )

            params = None

            returned += len(results)
            LOG.debug("yielding %d/%d %s", returned, total, endpoint)
            yield from results

    def list_crashes(
        self,
        query: Optional[Dict[str, Any]] = None,
        ordering: Optional[List[str]] = None,
    ):
        """List all CrashEntry objects.

        Arguments:
            query: The query definition to use.
                   (see crashmanager.views.json_to_query)
            ordering: Field(s) to order by (eg. `id` or `-id`)

        Yields:
            dict: Dict representation of CrashEntry
        """
        yield from self._list_objs("crashes", query=query, ordering=ordering)

    def list_buckets(self, query: Optional[Dict[str, Any]] = None):
        """List all Bucket objects.

        Arguments:
            query: The query definition to use.
                   (see crashmanager.views.json_to_query)

        Yields:
            dict: Dict representation of Bucket
        """
        yield from self._list_objs("buckets", query=query)

    @remote_checks
    def update_testcase_quality(self, crash_id: int, testcase_quality: int) -> None:
        """Update a CrashEntry's testcase quality.

        Arguments:
            crash_id: Crash ID to update.
            testcase_quality: Testcase quality to set.
        """
        url = (
            f"{self.serverProtocol}://{self.serverHost}:{self.serverPort}"
            f"/crashmanager/rest/crashes/{crash_id}/"
        )
        self.patch(url, data={"testcase_quality": testcase_quality})


class ReductionWorkflow(ABC):
    """Common framework for reduction scripts."""

    @abstractmethod
    def run(self) -> Optional[int]:
        """Run the actual reduction script.
        Any necessary parameters must be set on the instance in `from_args`/`__init__`.

        Returns:
            Return code (0 for success)
        """

    @staticmethod
    @abstractmethod
    def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
        """Parse CLI arguments and return the parsed result.

        This should used `CommonArgParser` to ensure the default arguments exist for
        compatibility with `main`.

        Arguments:
            Arguments list from shell (None for sys.argv).

        Returns:
            Parsed args.
        """

    @classmethod
    @abstractmethod
    def from_args(cls, args: argparse.Namespace) -> "ReductionWorkflow":
        """Create an instance from parsed args.

        Arguments:
            Parsed args.
        """

    @staticmethod
    def ensure_credentials() -> None:
        """Ensure necessary credentials exist for reduction scripts.

        This checks:
            ~/.fuzzmanagerconf  -- fuzzmanager credentials
        """
        # get fuzzmanager config from taskcluster
        conf_path = Path.home() / ".fuzzmanagerconf"
        if not conf_path.is_file():
            key = Taskcluster.load_secrets("project/fuzzing/fuzzmanagerconf")["key"]
            conf_path.write_text(key)
            conf_path.chmod(0o400)

    @classmethod
    def main(cls, args: Optional[argparse.Namespace] = None) -> Optional[int]:
        """Main entrypoint for reduction scripts."""
        if args is None:
            args = cls.parse_args()

        assert args is not None
        # Setup logger
        basicConfig(level=args.log_level)

        # Setup credentials if needed
        cls.ensure_credentials()

        return cls.from_args(args).run()
