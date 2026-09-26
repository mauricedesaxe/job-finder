from __future__ import annotations

import argparse
import sys
from typing import cast

from job_finder.benchmarks.qualification_execution import execute_qualification_evidence
from job_finder.benchmarks.qualification_evidence import Phase
from job_finder.evaluation.qualification_components import QualificationTargetId
from scripts.serve_mcp import create_dependencies


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one frozen qualification evidence request")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument(
        "--phase",
        required=True,
        choices=("input_preparation", "relevance", "enrichment", "deduplication", "composition"),
    )
    parser.add_argument("--input-id", required=True)
    args = parser.parse_args()
    dependencies = create_dependencies()
    if dependencies.implementation_artifact_path is None:
        parser.error("Executing build artifact is not configured")
    if dependencies.resolve_provider_credentials is None:
        parser.error("Provider credential resolver is not configured")
    with dependencies.connect() as connection:
        result = execute_qualification_evidence(
            connection,
            idempotency_key=cast(str, args.idempotency_key),
            target_id=QualificationTargetId(cast(str, args.target_id)),
            phase=cast(Phase, args.phase),
            input_id=cast(str, args.input_id),
            artifact_path=dependencies.implementation_artifact_path,
            resolve_credentials=dependencies.resolve_provider_credentials,
            completed_at=dependencies.now(),
            created_by=dependencies.actor,
        )
    sys.stdout.write(result.model_dump_json() + "\n")
    if result.state == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
