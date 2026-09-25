# pyright: reportMissingTypeStubs=false
"""Guard the production composition root against silently missing services.

create_review_app defaults every injectable service to a stub that raises
OperationsUnavailable at request time. That is convenient for tests and a
trap for production: a service left unwired in serve_review.py turns its
page into a 503 only after deploy. These tests pin the wiring.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from job_finder.review.app import create_review_app

SERVE_REVIEW = Path(__file__).parents[1] / "scripts" / "serve_review.py"


def _serve_review_supplied_parameters() -> set[str]:
    """Parameter names of create_review_app supplied by serve_review's call."""

    call = next(
        node
        for node in ast.walk(ast.parse(SERVE_REVIEW.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_review_app"
    )
    parameters = list(inspect.signature(create_review_app).parameters)
    supplied = {parameters[index] for index in range(min(len(call.args), len(parameters)))}
    supplied.update(keyword.arg for keyword in call.keywords if keyword.arg is not None)
    return supplied


def test_serve_review_wires_every_service_parameter() -> None:
    service_parameters = {
        name
        for name in inspect.signature(create_review_app).parameters
        if name.endswith("_service")
    }
    assert service_parameters, "no service parameters found; the test is broken"

    supplied = _serve_review_supplied_parameters()

    missing = sorted(service_parameters - supplied)
    assert (
        not missing
    ), f"serve_review.py never wires {missing}; those pages would 503 in production"
