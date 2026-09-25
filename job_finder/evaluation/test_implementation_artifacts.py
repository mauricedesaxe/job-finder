from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from job_finder.evaluation.implementation_artifacts import (
    ImplementationArtifact,
    build_implementation_artifact,
    load_implementation_artifact,
    write_implementation_artifact,
)


def test_build_artifact_identifies_source_and_dependency_closure(tmp_path: Path) -> None:
    (tmp_path / "job_finder").mkdir()
    (tmp_path / "scripts").mkdir()
    for name in ("Dockerfile", "pyproject.toml", "dagster.yaml", "workspace.yaml"):
        (tmp_path / name).write_text(name)
    (tmp_path / "uv.lock").write_text("locked dependencies")
    source = tmp_path / "job_finder" / "executor.py"
    source.write_text("result = 1\n")
    (tmp_path / "scripts" / "serve_review.py").write_text("app = None\n")

    first = write_implementation_artifact(tmp_path, tmp_path / "artifact.json")
    assert load_implementation_artifact(tmp_path / "artifact.json") == first
    assert build_implementation_artifact(tmp_path).id == first.id

    source.write_text("result = 2\n")
    second = build_implementation_artifact(tmp_path)
    assert second.id != first.id

    (tmp_path / "uv.lock").write_text("changed dependencies")
    assert build_implementation_artifact(tmp_path).id != second.id

    with pytest.raises(ValidationError):
        _ = ImplementationArtifact(id=second.id, manifest=first.manifest)
