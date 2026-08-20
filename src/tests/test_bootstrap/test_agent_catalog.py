from __future__ import annotations

from voidx.bootstrap.agent_catalog import tool_catalog


def test_tool_catalog_enumerates_builtin_and_agent_tools() -> None:
    tools = tool_catalog()
    ids = {tool["id"] for tool in tools}
    # Builtin file/shell tools
    assert {"read", "write", "manage", "replace", "find", "search", "git", "bash"} <= ids
    # Agent orchestration tools
    assert {"clarify", "checkpoint", "workflow", "todo", "agent", "agent_control"} <= ids
    for tool in tools:
        assert tool["id"].strip()
        assert isinstance(tool["description"], str)
