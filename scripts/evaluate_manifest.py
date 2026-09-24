from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

import psycopg

from job_finder.benchmarks.executions import (
    CompletedEvaluationExecution,
    EvaluateManifestCommand,
    FailedEvaluationExecution,
)
from job_finder.evaluation.manifest_execution import run_stored_manifest
from job_finder.evaluation.prompt_releases import (
    bootstrap_prompt_release,
)
from job_finder.evaluation.models import ReleaseTarget
from job_finder.evaluation.relevance_releases import (
    build_gemini_policy,
    build_jev_atomic_policy,
    build_jev_faithful_policy,
    build_relevance_release,
    store_relevance_release,
)

Provider = Literal["gemini", "jev-faithful", "jev-atomic"]


class Arguments(argparse.Namespace):
    manifest_id: str = ""
    provider: Provider = "gemini"
    idempotency_key: str = ""
    implementation_ref: str = ""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    postgres_dsn = _required_environment("JOB_FINDER_POSTGRES_DSN")
    observed_at = datetime.now(UTC)

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        release = bootstrap_prompt_release(connection)
        relevance_release = store_relevance_release(
            connection,
            build_relevance_release(
                build_gemini_policy(release)
                if arguments.provider == "gemini"
                else (
                    build_jev_atomic_policy()
                    if arguments.provider == "jev-atomic"
                    else build_jev_faithful_policy(release)
                )
            ),
            created_at=observed_at,
            created_by="evaluate_manifest",
        )
        target = ReleaseTarget(
            prompt_release_id=release.id,
            relevance_release_id=relevance_release.id,
        )
        execution = run_stored_manifest(
            connection,
            EvaluateManifestCommand(
                idempotency_key=arguments.idempotency_key,
                manifest_id=arguments.manifest_id,
                target=target,
                implementation_ref=arguments.implementation_ref,
            ),
        )

    payload: dict[str, object] = {
        "provider": arguments.provider,
        "execution": execution.model_dump(mode="json"),
    }
    if isinstance(execution, CompletedEvaluationExecution):
        payload["run"] = execution.run.model_dump(mode="json")
        if execution.telemetry is not None:
            payload["telemetry"] = execution.telemetry.model_dump(mode="json")
    elif isinstance(execution, FailedEvaluationExecution):
        payload["failure"] = execution.failure.model_dump(mode="json")
        if execution.telemetry is not None:
            payload["telemetry"] = execution.telemetry.model_dump(mode="json")
    print(json.dumps(payload, indent=2, default=str))
    return 1 if isinstance(execution, FailedEvaluationExecution) else 0


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _parse_arguments(argv: Sequence[str] | None) -> Arguments:
    parser = argparse.ArgumentParser(description="Run one provider against a frozen manifest.")
    _ = parser.add_argument("--manifest-id", required=True)
    _ = parser.add_argument(
        "--provider", required=True, choices=("gemini", "jev-faithful", "jev-atomic")
    )
    _ = parser.add_argument("--idempotency-key", required=True)
    _ = parser.add_argument("--implementation-ref", required=True)
    return parser.parse_args(argv, namespace=Arguments())


if __name__ == "__main__":
    raise SystemExit(main())
