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

    assert [edge for edge in debt if edge.get("remove_by") == "P7"] == []


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
