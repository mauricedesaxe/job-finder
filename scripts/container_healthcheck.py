from __future__ import annotations

import sys
import urllib.request

from dagster import DagsterInstance


def main() -> int:
    if sys.argv[1:] == ["dagster-webserver"]:
        request = urllib.request.Request(
            "http://127.0.0.1:3000/graphql",
            data=b'{"query":"{ repositoriesOrError { __typename } }"}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            body = response.read()
        return 0 if b'"RepositoryConnection"' in body else 1
    if sys.argv[1:] == ["dagster-daemon"]:
        with DagsterInstance.get() as instance:
            statuses = instance.get_daemon_statuses()
        return 0 if statuses and all(status.healthy for status in statuses.values()) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
