from __future__ import annotations

import ast
from pathlib import Path


_FRAMEWORK_ROOTS = frozenset({"dagster", "fasthtml", "fastmcp", "langfuse", "mcp", "starlette"})
_ALLOWED_FRAMEWORK_IMPORTS = {
    ("job_finder/dagster.py", "dagster"),
    ("job_finder/mcp_server.py", "fastmcp"),
    ("job_finder/mcp_server.py", "mcp"),
    ("job_finder/projections/langfuse.py", "langfuse"),
    ("job_finder/projections/smoke.py", "langfuse"),
    ("job_finder/review/app.py", "fasthtml"),
    ("job_finder/review/app.py", "starlette"),
    ("job_finder/review/configuration_editor.py", "fasthtml"),
    ("job_finder/review/configuration_editor.py", "starlette"),
    ("job_finder/review/workbench.py", "fasthtml"),
    ("job_finder/review/workbench.py", "starlette"),
    ("job_finder/web/app.py", "fasthtml"),
    ("job_finder/web/app.py", "starlette"),
    ("job_finder/web/security.py", "starlette"),
    ("job_finder/web/shell.py", "fasthtml"),
    ("job_finder/web/shell.py", "starlette"),
}
_WEB_PUBLIC_SYMBOLS = {
    "app.py": frozenset({"ReadinessProbe", "RequestGuard", "create_web_app", "static_url"}),
    "security.py": frozenset(
        {
            "SecurityHeadersMiddleware",
            "authenticate_session",
            "csrf_token",
            "ensure_csrf_token",
            "form_text",
            "valid_csrf",
            "verified_control_csrf_token",
            "verified_csrf_token",
        }
    ),
    "shell.py": frozenset(
        {
            "OperationsPage",
            "ShellSection",
            "absolute_time",
            "document",
            "operations_sidebar_page",
            "operations_sub_sidebar",
            "relative_time",
            "sidebar",
            "sidebar_page",
            "state_response",
            "timestamp",
        }
    ),
}
_BENCHMARK_PUBLIC_SYMBOLS = {
    "manifests.py": frozenset(
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
            "exclude_review_event",
            "include_review_event",
            "list_manifests",
            "load_manifest",
            "preview_manifest",
            "summarize_manifest",
        }
    ),
    "scoring.py": frozenset(
        {
            "EvaluationMetrics",
            "EvaluationTrialResult",
            "score_results",
            "score_trial",
        }
    ),
    "executions.py": frozenset(
        {
            "CaseEvaluator",
            "CaseEvaluatorFactory",
            "CompletedEvaluationExecution",
            "EvaluateManifestCommand",
            "EvaluationExecution",
            "EvaluationExecutionFailure",
            "EvaluationExecutionState",
            "EvaluationRun",
            "EvaluationRunTelemetry",
            "FailedEvaluationExecution",
            "LegacyEvaluateManifestCommand",
            "RequestObservationRecorder",
            "RunningEvaluationExecution",
            "aggregate_evaluation_telemetry",
            "exchange_rate_snapshot_digest",
            "load_evaluation_execution",
            "load_evaluation_execution_by_key",
            "load_run",
            "run_manifest",
        }
    ),
    "comparisons.py": frozenset(
        {
            "EvaluationCaseTransition",
            "EvaluationRunComparison",
            "EvaluationTrialTransition",
            "compare_runs",
            "preview_run_comparison",
            "promotion_eligibility_failures",
        }
    ),
    "promotions.py": frozenset(
        {
            "PromptPromotionDecision",
            "load_promotion_decision",
            "record_prompt_promotion_decision",
        }
    ),
}
_PROJECTION_PUBLIC_SYMBOLS = {
    "outbox.py": frozenset(
        {
            "LangfuseProjection",
            "LangfuseProjectionResponse",
            "LangfuseUnavailable",
            "ProjectionDelivered",
            "ProjectionDeliveryResult",
            "ProjectionFailed",
            "ProjectionFailureSummary",
            "ProjectionIdle",
            "ProjectionKind",
            "ProjectionLeaseLost",
            "ProjectionModel",
            "ProjectionQueueStatus",
            "ProjectionSender",
            "TypedProjectionSender",
            "deliver_next_projection",
            "enqueue_projection",
            "load_projection_queue_status",
        }
    ),
    "langfuse.py": frozenset(
        {
            "CreateDataset",
            "CreateDatasetItem",
            "LangfuseGateway",
            "ObservationProjection",
            "ObservationUsage",
            "SendObservation",
            "create_langfuse_projection_sender",
        }
    ),
    "rebuild.py": frozenset({"rebuild_langfuse_projections"}),
    "smoke.py": frozenset({"run_live_smoke"}),
}
_PACKAGE_ROOT = Path(__file__).parent
_REPOSITORY_ROOT = _PACKAGE_ROOT.parent
_OLD_MANIFEST_MODULE = "job_finder.evaluation.manifests"
_OLD_LANGFUSE_MODULE = "job_finder.evaluation.langfuse"
_OLD_REVIEW_SHELL_MODULE = "job_finder.review.shell"


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


def _direct_imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(_absolute_from_module(path, node))
    return modules


def _public_definitions(path: Path) -> set[str]:
    names: set[str] = set()
    for node in _parse(path).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return {name for name in names if not name.startswith("_")}


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


def test_web_modules_own_shared_http_adapters() -> None:
    web_root = _PACKAGE_ROOT / "web"
    assert (web_root / "__init__.py").read_text() == ""
    assert not (_PACKAGE_ROOT / "review" / "shell.py").exists()
    assert not (_PACKAGE_ROOT / "review" / "static").exists()

    source_roots = (_PACKAGE_ROOT, _REPOSITORY_ROOT / "contracts", _REPOSITORY_ROOT / "scripts")
    stale_imports = {
        path.relative_to(_REPOSITORY_ROOT).as_posix()
        for source_root in source_roots
        for path in source_root.rglob("*.py")
        if _OLD_REVIEW_SHELL_MODULE in _imported_modules(path)
    }
    assert not stale_imports

    for filename, expected_symbols in _WEB_PUBLIC_SYMBOLS.items():
        path = web_root / filename
        assert _public_definitions(path) == expected_symbols
        assert not any(
            module == "job_finder.review" or module.startswith("job_finder.review.")
            for module in _direct_imported_modules(path)
        )


def test_review_workbench_owns_only_the_review_web_adapter() -> None:
    path = _PACKAGE_ROOT / "review" / "workbench.py"
    assert _public_definitions(path) == {"ReviewWorkbench"}
    assert not {
        module
        for module in _imported_modules(path)
        if module == "job_finder.database"
        or module.rpartition(".")[2] in {"load_review_queue", "record_review"}
        or module.rpartition(".")[2].startswith("postgres_review_")
    }


def test_benchmark_modules_own_their_public_symbols() -> None:
    benchmark_root = _PACKAGE_ROOT / "benchmarks"
    assert (benchmark_root / "__init__.py").read_text() == ""
    assert (_PACKAGE_ROOT / "evaluation" / "__init__.py").read_text() == ""
    assert not (_PACKAGE_ROOT / "evaluation" / "manifests.py").exists()
    for filename, expected_symbols in _BENCHMARK_PUBLIC_SYMBOLS.items():
        assert _public_definitions(benchmark_root / filename) == expected_symbols

    source_roots = (_PACKAGE_ROOT, _REPOSITORY_ROOT / "contracts", _REPOSITORY_ROOT / "scripts")
    stale_imports = {
        path.relative_to(_REPOSITORY_ROOT).as_posix()
        for source_root in source_roots
        for path in source_root.rglob("*.py")
        if _OLD_MANIFEST_MODULE in _imported_modules(path)
    }
    assert not stale_imports

    for path in (
        _PACKAGE_ROOT / "evaluation" / "evaluate.py",
        _PACKAGE_ROOT / "evaluation" / "jev.py",
        _PACKAGE_ROOT / "evaluation" / "openrouter.py",
        _PACKAGE_ROOT / "evaluation" / "release_targets.py",
    ):
        assert not any(
            module == "job_finder.benchmarks" or module.startswith("job_finder.benchmarks.")
            for module in _imported_modules(path)
        )


def test_projection_modules_own_the_projection_contract() -> None:
    projection_root = _PACKAGE_ROOT / "projections"
    assert (projection_root / "__init__.py").read_text() == ""
    assert not (_PACKAGE_ROOT / "evaluation" / "langfuse.py").exists()
    for filename, expected_symbols in _PROJECTION_PUBLIC_SYMBOLS.items():
        assert _public_definitions(projection_root / filename) == expected_symbols

    source_roots = (_PACKAGE_ROOT, _REPOSITORY_ROOT / "contracts", _REPOSITORY_ROOT / "scripts")
    stale_imports = {
        path.relative_to(_REPOSITORY_ROOT).as_posix()
        for source_root in source_roots
        for path in source_root.rglob("*.py")
        if _OLD_LANGFUSE_MODULE in _imported_modules(path)
    }
    assert not stale_imports


def test_projection_outbox_has_no_payload_or_sdk_dependencies() -> None:
    projection_root = _PACKAGE_ROOT / "projections"
    outbox_imports = _direct_imported_modules(projection_root / "outbox.py")
    assert "langfuse" not in outbox_imports
    assert not any(
        module.startswith(("job_finder.benchmarks", "job_finder.config", "job_finder.evaluation"))
        for module in outbox_imports
    )


def test_langfuse_sdk_stays_in_projection_adapters() -> None:
    source_roots = (_PACKAGE_ROOT, _REPOSITORY_ROOT / "contracts", _REPOSITORY_ROOT / "scripts")
    sdk_importers = {
        path.relative_to(_REPOSITORY_ROOT).as_posix()
        for source_root in source_roots
        for path in source_root.rglob("*.py")
        if not path.name.startswith("test_")
        and any(
            module == "langfuse" or module.startswith("langfuse.")
            for module in _direct_imported_modules(path)
        )
    }
    assert sdk_importers == {
        "job_finder/projections/langfuse.py",
        "job_finder/projections/smoke.py",
    }


def test_projection_imports_point_toward_the_outbox() -> None:
    openrouter_imports = _direct_imported_modules(_PACKAGE_ROOT / "evaluation" / "openrouter.py")
    assert not any(module.startswith("job_finder.projections") for module in openrouter_imports)

    for filename in ("manifests.py", "executions.py", "promotions.py"):
        imports = _direct_imported_modules(_PACKAGE_ROOT / "benchmarks" / filename)
        assert {module for module in imports if module.startswith("job_finder.projections")} == {
            "job_finder.projections.outbox"
        }


def test_projection_scripts_only_wire_package_operations() -> None:
    scripts = {
        "rebuild_langfuse_projections.py": "job_finder.projections.rebuild",
        "smoke_langfuse_projection.py": "job_finder.projections.smoke",
    }
    forbidden_implementation = {
        "rebuild_langfuse_projections.py": (
            "langfuse_projection_items",
            "ModelCallAttempt",
            "SELECT ",
            "TypeAdapter",
        ),
        "smoke_langfuse_projection.py": (
            "hashlib",
            "LangfuseProjection",
            "ModelCallAttempt",
            "TypeAdapter",
        ),
    }
    for filename, expected_projection_import in scripts.items():
        path = _REPOSITORY_ROOT / "scripts" / filename
        assert {
            module
            for module in _direct_imported_modules(path)
            if module.startswith("job_finder.projections")
        } == {expected_projection_import}
        assert {
            node.name
            for node in _parse(path).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        } == {"main"}
        assert not any(value in path.read_text() for value in forbidden_implementation[filename])
