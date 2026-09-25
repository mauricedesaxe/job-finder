from __future__ import annotations

from pathlib import Path

from job_finder.evaluation.implementation_artifacts import write_implementation_artifact


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    artifact = write_implementation_artifact(root, root / "implementation-artifact.json")
    print(artifact.id)
