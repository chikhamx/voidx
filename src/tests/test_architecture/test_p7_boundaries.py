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
    )

    assert [path for path in expected if not (ROOT / path).is_file()] == []
    assert [path for path in legacy if (ROOT / path).exists()] == []


def test_p7_architecture_debt_is_removed() -> None:
    import json

    debt_path = ROOT / "src/tests/fixtures/architecture/current_edges.json"
    debt = json.loads(debt_path.read_text(encoding="utf-8"))

    assert [edge for edge in debt if edge.get("remove_by") == "P7"] == []
