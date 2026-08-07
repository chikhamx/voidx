from __future__ import annotations

from tests.tool_registry import build_registry

from .snapshot import assert_snapshot


def test_tool_catalog_contract() -> None:
    registry = build_registry()
    assert_snapshot("tool_catalog.json", registry.serialize_definitions())


async def test_tool_result_contract(tmp_path) -> None:
    from voidx.tooling.builtin.file.read import FileReadTool
    from voidx.tooling.builtin.shell.bash.tool import BashTool
    from voidx.tooling.application.execution import FileToolContext, ShellToolContext
    from voidx.tooling.domain.context import ToolExecutionContext
    from voidx.tooling.domain.result import ToolResult, tool_timeout_metadata

    workspace = str(tmp_path)
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    context = FileToolContext(workspace=workspace)
    registry = build_registry()
    cases = {
        "file_read_success": await FileReadTool().execute(
            {"file_path": str(tmp_path / "sample.txt"), "offset": None, "limit": None},
            context,
        ),
        "file_read_invalid": await FileReadTool().execute({"file_path": 123}, context),
        "shell_invalid_workspace": await BashTool().execute(
            {"command": "echo hello"}, ShellToolContext(workspace="")
        ),
        "unknown_tool": await registry.execute_tool("missing", {}, context),
        "denied": ToolResult.denied("Permission denied: write"),
        "unavailable": ToolResult.unavailable("LSP server unavailable"),
        "timeout": ToolResult(
            output="Command timed out after 1 seconds.",
            metadata=tool_timeout_metadata("shell", exit_code=-1),
        ),
    }

    expected_fields = {
        "title",
        "output",
        "summary",
        "metadata",
        "diff",
        "next_step_hint",
        "display",
    }
    for result in cases.values():
        assert set(result.model_dump(mode="json")) == expected_fields

    def normalize(value):
        if isinstance(value, str):
            return value.replace(workspace, "${WORKSPACE}")
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    assert_snapshot(
        "tool_results.json",
        {name: normalize(result.model_dump(mode="json")) for name, result in cases.items()},
    )
