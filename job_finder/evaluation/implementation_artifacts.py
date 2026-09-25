from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal, NewType, Self

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator
from psycopg.types.json import Jsonb

ImplementationArtifactId = NewType("ImplementationArtifactId", str)
_DIGEST = r"^[0-9a-f]{64}$"
_SOURCE_DIRS = ("job_finder", "scripts")
_BUILD_FILES = ("Dockerfile", "pyproject.toml", "dagster.yaml", "workspace.yaml")
_ENTRYPOINTS = (
    "scripts.serve_review:create_app",
    "job_finder.dagster:defs",
)


class ArtifactModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ArtifactFile(ArtifactModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_DIGEST)


class ImplementationManifest(ArtifactModel):
    schema_version: Literal[1] = 1
    runtime: str = Field(min_length=1)
    dependency_lock_sha256: str = Field(pattern=_DIGEST)
    entrypoints: tuple[str, ...] = Field(min_length=1)
    source_files: tuple[ArtifactFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def files_are_sorted_and_unique(self) -> Self:
        paths = tuple(file.path for file in self.source_files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("Artifact source files must be sorted and unique")
        return self


class ImplementationArtifact(ArtifactModel):
    id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    manifest: ImplementationManifest

    @model_validator(mode="after")
    def identity_matches_manifest(self) -> Self:
        if self.id != implementation_artifact_id(self.manifest):
            raise ValueError("Implementation artifact identity differs from manifest")
        return self


def implementation_artifact_id(manifest: ImplementationManifest) -> ImplementationArtifactId:
    content = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return ImplementationArtifactId(hashlib.sha256(content.encode()).hexdigest())


def build_implementation_artifact(root: Path) -> ImplementationArtifact:
    if not (root / "uv.lock").is_file():
        raise ValueError("Implementation artifact build requires uv.lock")
    paths = [root / name for name in _BUILD_FILES]
    for directory in _SOURCE_DIRS:
        paths.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    if any(not path.is_file() for path in paths):
        raise ValueError("Implementation artifact build is missing a source file")
    source_files = tuple(
        ArtifactFile(
            path=path.relative_to(root).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    )
    manifest = ImplementationManifest(
        runtime=f"Python {platform.python_version()} ({platform.machine()})",
        dependency_lock_sha256=hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        entrypoints=_ENTRYPOINTS,
        source_files=source_files,
    )
    return ImplementationArtifact(id=implementation_artifact_id(manifest), manifest=manifest)


def write_implementation_artifact(root: Path, destination: Path) -> ImplementationArtifact:
    artifact = build_implementation_artifact(root)
    destination.write_text(artifact.model_dump_json(indent=2) + "\n")
    return artifact


def load_implementation_artifact(path: Path) -> ImplementationArtifact:
    return ImplementationArtifact.model_validate_json(path.read_text())


def store_implementation_artifact(
    connection: psycopg.Connection[tuple[object, ...]],
    artifact: ImplementationArtifact,
    *,
    created_at: datetime,
    created_by: str,
) -> ImplementationArtifact:
    _ = connection.execute(
        """
        INSERT INTO implementation_artifacts (id, manifest, created_at, created_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (artifact.id, Jsonb(artifact.manifest.model_dump(mode="json")), created_at, created_by),
    )
    row = connection.execute(
        "SELECT manifest FROM implementation_artifacts WHERE id = %s", (artifact.id,)
    ).fetchone()
    if row is None or ImplementationManifest.model_validate(row[0]) != artifact.manifest:
        raise ValueError("Stored implementation artifact differs from its identity")
    return artifact
