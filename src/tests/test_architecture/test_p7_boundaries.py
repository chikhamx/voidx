"""Architecture boundaries introduced by phase P7."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_foundation_modules_have_final_owners() -> None:
    expected = (
        "src/voidx/observability/__init__.py",
        "src/voidx/observability/external.py",
        "src/voidx/observability/internal_error.py",
        "src/voidx/observability/request_log.py",
        "src/voidx/observability/tool_log.py",
        "src/voidx/update/__init__.py",
        "src/voidx/update/service.py",
        "src/voidx/platform/execution_context.py",
    )
    legacy = (
        "src/voidx/logging",
        "src/voidx/selfupdate.py",
        "src/voidx/runtime/execution_context.py",
        "src/voidx/agent/runtime",
    )

    assert [path for path in expected if not (ROOT / path).is_file()] == []
    assert [path for path in legacy if (ROOT / path).exists()] == []


def test_p7_architecture_debt_is_removed() -> None:
    import json

    debt_path = ROOT / "src/tests/fixtures/architecture/current_edges.json"
    debt = json.loads(debt_path.read_text(encoding="utf-8"))

    assert debt == []


def test_agent_non_langgraph_adapters_have_final_owners() -> None:
    expected = (
        "src/voidx/presentation/tools/clipboard_image.py",
        "src/voidx/agent/adapters/input_adapter.py",
        "src/voidx/agent/adapters/input_router.py",
        "src/voidx/agent/adapters/null_events.py",
        "src/voidx/agent/adapters/presentation_adapter.py",
        "src/voidx/agent/adapters/persistence/memory_session.py",
        "src/voidx/agent/adapters/persistence/message_rows.py",
        "src/voidx/agent/adapters/persistence/runtime_state_mapper.py",
        "src/voidx/agent/adapters/tools/result_storage.py",
    )
    legacy = tuple(
        f"src/voidx/agent/infrastructure/{name}.py"
        for name in (
            "clipboard_image",
            "input_adapter",
            "input_router",
            "memory_session",
            "message_rows",
            "null_events",
            "presentation_adapter",
            "runtime_state_mapper",
            "tool_result_storage",
        )
    )

    assert [path for path in expected if not (ROOT / path).is_file()] == []
    assert [path for path in legacy if (ROOT / path).exists()] == []


def test_langgraph_adapters_have_final_owner() -> None:
    expected = (
        "src/voidx/agent/adapters/langgraph/__init__.py",
        "src/voidx/agent/adapters/langgraph/execution.py",
        "src/voidx/agent/domain/display_policy.py",
        "src/voidx/agent/adapters/langgraph/graph_compaction.py",
        "src/voidx/agent/domain/ui_events.py",
    )

    assert [path for path in expected if not (ROOT / path).is_file()] == []
    assert not (ROOT / "src/voidx/agent/infrastructure").exists()


def test_slash_commands_are_owned_by_presentation() -> None:
    expected = (
        "src/voidx/presentation/slash/__init__.py",
        "src/voidx/presentation/slash/handler.py",
        "src/voidx/presentation/slash/commands/__init__.py",
    )

    assert [path for path in expected if not (ROOT / path).is_file()] == []
    assert not (ROOT / "src/voidx/agent/slash").exists()


def test_main_depends_only_on_bootstrap() -> None:
    import ast

    path = ROOT / "src/voidx/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert imports == {"voidx.bootstrap"}


def test_all_legacy_package_directories_are_removed() -> None:
    legacy = (
        "runtime",
        "workflow",
        "memory",
        "permission",
        "tools",
        "ui",
        "logging",
        "agent/goal",
        "agent/loop",
        "agent/runtime",
        "agent/infrastructure",
        "agent/gateway",
        "agent/slash",
    )

    assert [path for path in legacy if (ROOT / "src/voidx" / path).exists()] == []


LEGACY_MODULE_PREFIXES = (
    "voidx.runtime",
    "voidx.workflow",
    "voidx.memory",
    "voidx.permission",
    "voidx.tools",
    "voidx.ui",
    "voidx.logging",
    "voidx.selfupdate",
    "voidx.agent.goal",
    "voidx.agent.loop",
    "voidx.agent.runtime",
    "voidx.agent.infrastructure",
    "voidx.agent.gateway",
    "voidx.agent.slash",
)


def _is_legacy_module(value: str) -> bool:
    return any(value == prefix or value.startswith(f"{prefix}.") for prefix in LEGACY_MODULE_PREFIXES)


def test_executable_python_has_no_legacy_import_or_patch_targets() -> None:
    import ast

    offenders: list[str] = []
    for root_name in ("src", "tui", "scripts"):
        for path in (ROOT / root_name).rglob("*.py"):
            if "__pycache__" in path.parts or "build" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                values: list[str] = []
                if isinstance(node, ast.Import):
                    values = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    values = [node.module]
                elif isinstance(node, ast.Call):
                    function_name = ""
                    if isinstance(node.func, ast.Name):
                        function_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        function_name = node.func.attr
                    if function_name in {"import_module", "setattr", "delattr"}:
                        values = [
                            arg.value
                            for arg in node.args
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        ]
                for value in values:
                    if _is_legacy_module(value):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {value}")

    assert offenders == []
