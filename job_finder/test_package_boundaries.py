from __future__ import annotations

import ast
from pathlib import Path


_FRAMEWORK_ROOTS = frozenset({"dagster", "fasthtml", "fastmcp", "langfuse", "mcp", "starlette"})
_ALLOWED_FRAMEWORK_IMPORTS = {
    ("job_finder/dagster.py", "dagster"),
    ("job_finder/evaluation/langfuse.py", "langfuse"),
    ("job_finder/mcp_server.py", "fastmcp"),
    ("job_finder/mcp_server.py", "mcp"),
    ("job_finder/review/app.py", "fasthtml"),
    ("job_finder/review/app.py", "starlette"),
    ("job_finder/review/configuration_editor.py", "fasthtml"),
    ("job_finder/review/configuration_editor.py", "starlette"),
    ("job_finder/review/shell.py", "fasthtml"),
}


def test_framework_sdks_stay_in_adapter_modules() -> None:
    package_root = Path(__file__).parent
    observed: set[tuple[str, str]] = set()

    for path in package_root.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.partition(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                roots = {node.module.partition(".")[0]}
            else:
                continue
            relative_path = path.relative_to(package_root.parent).as_posix()
            observed.update((relative_path, root) for root in roots & _FRAMEWORK_ROOTS)

    assert observed == _ALLOWED_FRAMEWORK_IMPORTS
