from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcp.types import ToolAnnotations

from job_finder.benchmarks.executions import EvaluateManifestCommand, EvaluationExecutionState
from job_finder.benchmarks.qualification_execution import CredentialResolver
from job_finder.database import Connection, ConnectionFactory

Clock = Callable[[], datetime]
EvaluationRunner = Callable[[Connection, EvaluateManifestCommand], EvaluationExecutionState]


@dataclass(frozen=True)
class McpDependencies:
    connect: ConnectionFactory
    actor: str = "mcp-owner"
    now: Clock = lambda: datetime.now(UTC)
    run_evaluation: EvaluationRunner | None = None
    implementation_artifact_path: Path | None = None
    resolve_provider_credentials: CredentialResolver | None = None


READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
APPEND_ONLY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
CAS_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
PUBLISH_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
PROVIDER_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
