from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Generic, Literal, NewType, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

PromptVersionId = NewType("PromptVersionId", str)
PromptReleaseId = NewType("PromptReleaseId", str)
InputDigest = NewType("InputDigest", str)
ModelRequestId = NewType("ModelRequestId", str)


class EvaluationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class EvaluationToolOutput(EvaluationModel):
    passed: bool = Field(alias="pass")
    reason: str


PromptOutputT = TypeVar("PromptOutputT", bound=EvaluationModel)


@dataclass(frozen=True)
class PromptAccepted(Generic[PromptOutputT]):
    prompt_name: str
    output: PromptOutputT


class CompletedModelCall(EvaluationModel):
    kind: Literal["completed"] = "completed"
    prompt_name: str
    parsed_output: dict[str, JsonValue]


class CriterionAccepted(EvaluationModel):
    kind: Literal["accepted"] = "accepted"
    prompt_name: str
    passed: bool
    reason: str


class OperationalError(EvaluationModel):
    prompt_name: str
    error_code: str
    reason: str


class RetryableOperationalError(OperationalError):
    kind: Literal["retryable_error"] = "retryable_error"
    retryability: Literal["retryable"] = "retryable"


class TerminalOperationalError(OperationalError):
    kind: Literal["terminal_error"] = "terminal_error"
    retryability: Literal["terminal"] = "terminal"


OperationalFailure = Annotated[
    RetryableOperationalError | TerminalOperationalError,
    Field(discriminator="kind"),
]
CriterionResult = Annotated[
    CriterionAccepted | RetryableOperationalError | TerminalOperationalError,
    Field(discriminator="kind"),
]


class Qualified(EvaluationModel):
    kind: Literal["qualified"] = "qualified"
    reason: str
    profile_name: str


class Rejected(EvaluationModel):
    kind: Literal["rejected"] = "rejected"
    reason: str


EvaluationResult = Annotated[
    Qualified | Rejected | RetryableOperationalError | TerminalOperationalError,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class ModelCallContext:
    processing_attempt_id: UUID
    pipeline_run_id: UUID
    prompt_release_id: PromptReleaseId
    operation_key: str
    input_digest: InputDigest


@dataclass(frozen=True)
class ModelCallAttempt:
    id: UUID
    context: ModelCallContext
    request_id: ModelRequestId
    attempt_number: int
    prompt_name: str
    prompt_version_id: PromptVersionId
    requested_model: str
    response_model: str | None
    provider_response_id: str | None
    status: Literal["accepted", "retryable_error", "terminal_error"]
    parsed_output: dict[str, JsonValue] | None
    raw_response: JsonValue | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: int
    error: dict[str, JsonValue] | None
    observed_at: datetime
