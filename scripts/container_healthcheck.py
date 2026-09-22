from __future__ import annotations

import sys
from http.client import HTTPConnection

from dagster import DagsterInstance


def main() -> int:
    if sys.argv[1:] == ["dagster-webserver"]:
        connection = HTTPConnection("127.0.0.1", 3000, timeout=2)
        try:
            connection.request(
                "POST",
                "/graphql",
                body=b'{"query":"{ repositoriesOrError { __typename } }"}',
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        return 0 if response.status == 200 and b'"RepositoryConnection"' in body else 1
    if sys.argv[1:] == ["dagster-daemon"]:
        with DagsterInstance.get() as instance:
            statuses = instance.get_daemon_statuses()
        return 0 if statuses and all(status.healthy for status in statuses.values()) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
