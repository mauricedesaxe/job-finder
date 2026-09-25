"""Provision a private Job Finder stack in a new Railway project."""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast


REPO = "mauricedesaxe/job-finder"
ROLES = ("dagster-webserver", "dagster-daemon", "review")
DATABASE_URL = "${{Postgres.DATABASE_URL}}"
DAGSTER_URL = "http://${{dagster-webserver.RAILWAY_PRIVATE_DOMAIN}}:3000/graphql"


def output(message: str, *, flush: bool = False) -> None:
    _ = sys.stdout.write(f"{message}\n")
    if flush:
        sys.stdout.flush()


def railway(*args: str, cwd: Path, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["railway", *args],
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"railway {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def railway_json(*args: str, cwd: Path) -> object:
    return cast(object, json.loads(railway(*args, "--json", cwd=cwd)))


def field(value: object, key: str) -> object:
    if not isinstance(value, dict):
        raise ValueError(f"Expected Railway JSON object with {key}")
    return cast(dict[str, object], value)[key]


def string_field(value: object, key: str) -> str:
    result = field(value, key)
    if not isinstance(result, str):
        raise ValueError(f"Expected Railway JSON string field {key}")
    return result


def set_variable(
    project_id: str, environment_id: str, service: str, key: str, value: str, cwd: Path
) -> None:
    _ = railway(
        "variable",
        "set",
        key,
        "--stdin",
        "--project",
        project_id,
        "--environment",
        environment_id,
        "--service",
        service,
        cwd=cwd,
        stdin=value,
    )


def configure_role(
    project_id: str,
    environment_id: str,
    service_id: str,
    role: str,
    cwd: Path,
) -> None:
    commands = {
        "dagster-webserver": (
            "sh -c 'uv run --no-sync python scripts/init_dagster_schema.py && "
            "exec uv run --no-sync dagster-webserver -h 0.0.0.0 -p 3000 -w workspace.yaml'"
        ),
        "dagster-daemon": (
            "sh -c 'uv run --no-sync python scripts/init_dagster_schema.py && "
            "exec uv run --no-sync dagster-daemon run -w workspace.yaml'"
        ),
        "review": (
            "sh -c 'exec uv run --no-sync uvicorn scripts.serve_review:create_app "
            "--factory --host 0.0.0.0 --port ${PORT:-8080}'"
        ),
    }
    deployment: dict[str, object] = {
        "startCommand": commands[role],
        "restartPolicyType": "ON_FAILURE",
    }
    if role == "review":
        deployment.update({"healthcheckPath": "/readyz", "healthcheckTimeout": 300})
    if role == "dagster-webserver":
        deployment.update({"healthcheckPath": "/", "healthcheckTimeout": 300})
    patch = {
        "services": {
            service_id: {
                "build": {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"},
                "deploy": deployment,
            }
        }
    }
    response = cast(
        object,
        json.loads(
            railway(
                "environment",
                "edit",
                "--project",
                project_id,
                "--environment",
                environment_id,
                "--message",
                f"Configure {role}",
                "--json",
                cwd=cwd,
                stdin=json.dumps(patch),
            )
        ),
    )
    if field(response, "committed") is not True:
        raise RuntimeError(f"Railway did not apply the {role} service configuration")


def service_ids(project_id: str, environment_id: str, cwd: Path) -> dict[str, str]:
    services = railway_json(
        "service", "list", "--project", project_id, "--environment", environment_id, cwd=cwd
    )
    if not isinstance(services, list):
        raise ValueError("Expected Railway service list")
    return {
        string_field(service, "name"): string_field(service, "id")
        for service in cast(list[object], services)
    }


def check_name_available(name: str, workspace: str, cwd: Path) -> None:
    projects = railway_json("list", cwd=cwd)
    if not isinstance(projects, list):
        raise ValueError("Expected Railway project list")
    for candidate in cast(list[object], projects):
        if (
            string_field(candidate, "name") == name
            and string_field(field(candidate, "workspace"), "id") == workspace
        ):
            raise ValueError(f"Railway project {name} already exists in this workspace")


def wait_for_deployment(project_id: str, environment_id: str, service: str, cwd: Path) -> None:
    deadline = time.monotonic() + 15 * 60
    while time.monotonic() < deadline:
        deployments = railway_json(
            "deployment",
            "list",
            "--project",
            project_id,
            "--environment",
            environment_id,
            "--service",
            service,
            "--limit",
            "1",
            cwd=cwd,
        )
        if not isinstance(deployments, list):
            raise ValueError("Expected Railway deployment list")
        if deployments:
            status = string_field(cast(object, deployments[0]), "status")
            if status == "SUCCESS":
                time.sleep(20)
                latest = railway_json(
                    "deployment",
                    "list",
                    "--project",
                    project_id,
                    "--environment",
                    environment_id,
                    "--service",
                    service,
                    "--limit",
                    "1",
                    cwd=cwd,
                )
                if (
                    isinstance(latest, list)
                    and latest
                    and string_field(cast(object, latest[0]), "status") == "SUCCESS"
                ):
                    return
            if status in {"FAILED", "CRASHED", "REMOVED", "SKIPPED"}:
                raise RuntimeError(f"{service} deployment ended in {status}")
        time.sleep(10)
    raise TimeoutError(f"{service} did not become healthy within 15 minutes")


def configure_variables(
    project_id: str, environment_id: str, ids: dict[str, str], cwd: Path
) -> str:
    bootstrap_token = secrets.token_urlsafe(32)
    session_secret = secrets.token_urlsafe(48)
    encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    for role in ROLES:
        set_variable(project_id, environment_id, role, "JOB_FINDER_POSTGRES_DSN", DATABASE_URL, cwd)
        set_variable(
            project_id, environment_id, role, "JOB_FINDER_ENABLE_SPLIT_EXECUTION", "true", cwd
        )
        if role != "review":
            set_variable(
                project_id,
                environment_id,
                role,
                "JOB_FINDER_DAGSTER_POSTGRES_DSN",
                f"{DATABASE_URL}?options=-csearch_path%3Ddagster",
                cwd,
            )
        set_variable(
            project_id,
            environment_id,
            role,
            "JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY",
            encryption_key,
            cwd,
        )
        configure_role(project_id, environment_id, ids[role], role, cwd)
    set_variable(
        project_id, environment_id, "review", "JOB_FINDER_DAGSTER_GRAPHQL_URL", DAGSTER_URL, cwd
    )
    set_variable(project_id, environment_id, "dagster-webserver", "PORT", "3000", cwd)
    set_variable(
        project_id,
        environment_id,
        "review",
        "JOB_FINDER_REVIEW_SESSION_SECRET",
        session_secret,
        cwd,
    )
    set_variable(
        project_id,
        environment_id,
        "review",
        "JOB_FINDER_BOOTSTRAP_TOKEN",
        bootstrap_token,
        cwd,
    )
    set_variable(
        project_id, environment_id, "review", "JOB_FINDER_REVIEW_COOKIE_SECURE", "true", cwd
    )
    return bootstrap_token


def deploy(name: str, workspace: str, repo: str, branch: str, existing_project: str | None) -> None:
    if not 1 <= len(name) <= 32:
        raise ValueError("Railway project names must be between 1 and 32 characters")
    with tempfile.TemporaryDirectory(prefix="job-finder-railway-") as directory:
        cwd = Path(directory)
        if existing_project is None:
            check_name_available(name, workspace, cwd)
            created = railway_json("init", "--name", name, "--workspace", workspace, cwd=cwd)
            project_id = string_field(created, "id")
        else:
            project_id = existing_project
        linked = railway_json(
            "link", "--project", project_id, "--environment", "production", cwd=cwd
        )
        environment_id = string_field(linked, "environmentId")
        if string_field(linked, "projectName") != name:
            raise ValueError("The selected Railway project has a different name")
        output(f"Railway project: https://railway.com/project/{project_id}", flush=True)

        if service_ids(project_id, environment_id, cwd):
            raise RuntimeError("Project already has services; refusing to replace existing secrets")

        _ = railway_json("add", "--database", "postgres", cwd=cwd)
        for role in ROLES:
            _ = railway_json("add", "--service", role, cwd=cwd)
        ids = service_ids(project_id, environment_id, cwd)

        bootstrap_token = configure_variables(project_id, environment_id, ids, cwd)

        output("Provisioned private services. Connecting the application source…", flush=True)
        for role in ROLES:
            _ = railway(
                "service",
                "source",
                "connect",
                "--project",
                project_id,
                "--environment",
                environment_id,
                "--service",
                role,
                "--repo",
                repo,
                "--branch",
                branch,
                cwd=cwd,
            )

        domain = railway_json(
            "domain",
            "--project",
            project_id,
            "--environment",
            environment_id,
            "--service",
            "review",
            "--port",
            "8080",
            cwd=cwd,
        )
        for role in ROLES:
            output(f"Waiting for {role}…", flush=True)
            wait_for_deployment(project_id, environment_id, role, cwd)

        review_domain = string_field(domain, "domain")
        review_url = (
            review_domain if review_domain.startswith("https://") else f"https://{review_domain}"
        )
        output(f"Review app: {review_url}")
        output(f"Owner bootstrap token: {bootstrap_token}")
        output("Open the review app with the owner and finish setup in the browser.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Unique project name for this owner")
    parser.add_argument("--workspace", required=True, help="Railway workspace ID")
    parser.add_argument("--repo", default=REPO, help="GitHub repository to deploy")
    parser.add_argument("--branch", default="main", help="GitHub branch to deploy")
    parser.add_argument("--project", help="Resume an empty project created by a failed attempt")
    args = parser.parse_args()
    project_arg = cast(str | None, args.project)
    try:
        deploy(
            cast(str, args.name),
            cast(str, args.workspace),
            cast(str, args.repo),
            cast(str, args.branch),
            project_arg,
        )
    except (RuntimeError, ValueError, TimeoutError) as exc:
        parser.exit(1, f"Deployment failed: {exc}\n")


if __name__ == "__main__":
    main()
