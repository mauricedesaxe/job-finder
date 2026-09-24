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
_MOVED_MANIFEST_SYMBOLS = frozenset(
    {
        "CuratedReviewEvent",
        "EvaluationCaseInput",
        "EvaluationManifest",
        "EvaluationManifestCase",
        "ManifestOperationError",
        "ManifestPolicy",
        "ManifestSummary",
        "ManifestSummaryPage",
        "create_manifest",
        "enqueue_projection",
        "exclude_review_event",
        "include_review_event",
        "list_manifests",
        "load_manifest",
        "preview_manifest",
        "summarize_manifest",
    }
)
_PACKAGE_ROOT = Path(__file__).parent
_REPOSITORY_ROOT = _PACKAGE_ROOT.parent
_OLD_MANIFEST_MODULE = "job_finder.evaluation.manifests"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _absolute_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = list(path.relative_to(_REPOSITORY_ROOT).with_suffix("").parts[:-1])
    prefix = package[: len(package) - node.level + 1]
    suffix = [] if node.module is None else node.module.split(".")
    return ".".join((*prefix, *suffix))


def _imported_modules(path: Path) -> set[str]:
    tree = _parse(path)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_from_module(path, node)
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names)
    modules.update(_dynamic_imported_modules(tree))
    return modules


def _bound_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in _parse(path).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)
    return names


def _attribute_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_name(node.value)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def _dynamic_imported_modules(tree: ast.Module) -> set[str]:
    import_functions = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_functions.update(
                f"{alias.asname or alias.name}.import_module"
                for alias in node.names
                if alias.name == "importlib"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            import_functions.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )
    return {
        value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _attribute_name(node.func) in import_functions
        and node.args
        and isinstance((value := node.args[0]), ast.Constant)
        and isinstance(value.value, str)
    }


def _old_manifest_aliases(path: Path, tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(
                alias.asname
                for alias in node.names
                if alias.name == _OLD_MANIFEST_MODULE and alias.asname is not None
            )
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_from_module(path, node)
            aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if f"{module}.{alias.name}" == _OLD_MANIFEST_MODULE
            )
    return aliases


def _stale_manifest_references(path: Path) -> set[str]:
    tree = _parse(path)
    aliases = _old_manifest_aliases(path, tree)
    stale: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and _absolute_from_module(path, node) == _OLD_MANIFEST_MODULE
        ):
            names = {alias.name for alias in node.names}
            stale.update(
                _MOVED_MANIFEST_SYMBOLS if "*" in names else names & _MOVED_MANIFEST_SYMBOLS
            )
        elif isinstance(node, ast.Attribute):
            name = _attribute_name(node)
            for prefix in (_OLD_MANIFEST_MODULE, *aliases):
                if name in {f"{prefix}.{symbol}" for symbol in _MOVED_MANIFEST_SYMBOLS}:
                    stale.add(name)
    if _OLD_MANIFEST_MODULE in _dynamic_imported_modules(tree):
        stale.add("dynamic-import")
    return stale


def test_framework_sdks_stay_in_adapter_modules() -> None:
    observed: set[tuple[str, str]] = set()

    for path in _PACKAGE_ROOT.rglob("*.py"):
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
            relative_path = path.relative_to(_REPOSITORY_ROOT).as_posix()
            observed.update((relative_path, root) for root in roots & _FRAMEWORK_ROOTS)

    assert observed == _ALLOWED_FRAMEWORK_IMPORTS


def test_benchmark_manifest_import_boundaries() -> None:
    owner_names = _bound_names(_PACKAGE_ROOT / "benchmarks" / "manifests.py")
    legacy_names = _bound_names(_PACKAGE_ROOT / "evaluation" / "manifests.py")
    assert _MOVED_MANIFEST_SYMBOLS <= owner_names
    assert _MOVED_MANIFEST_SYMBOLS.isdisjoint(legacy_names)

    source_roots = (_PACKAGE_ROOT, _REPOSITORY_ROOT / "contracts", _REPOSITORY_ROOT / "scripts")
    stale_imports = {
        f"{path.relative_to(_REPOSITORY_ROOT)}:{name}"
        for source_root in source_roots
        for path in source_root.rglob("*.py")
        for name in _stale_manifest_references(path)
    }
    assert not stale_imports

    for path in (
        _PACKAGE_ROOT / "evaluation" / "evaluate.py",
        _PACKAGE_ROOT / "evaluation" / "jev.py",
        _PACKAGE_ROOT / "evaluation" / "openrouter.py",
    ):
        assert not any(
            module == "job_finder.benchmarks" or module.startswith("job_finder.benchmarks.")
            for module in _imported_modules(path)
        )
