from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

PipelineRunStatus = Literal["running", "completed", "failed"]
FailureSource = Literal["pipeline", "job"]
WorkItemState = Literal["pending", "leased", "failed", "completed", "terminal_error"]
ActionableWorkState = Literal["failed", "terminal_error"]


class OperationsUnavailable(RuntimeError):
    pass


def error_summary(raw_error: object) -> str:
    if not isinstance(raw_error, dict):
        return "Failure details unavailable"
    error = cast(Mapping[str, object], raw_error)
    code = error.get("code")
    reason = error.get("reason")
    if isinstance(code, str) and isinstance(reason, str):
        return f"{code}: {reason}"
    if isinstance(reason, str):
        return reason
    if isinstance(code, str):
        return code
    return "Failure details unavailable"
