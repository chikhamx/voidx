from pathlib import Path

import pytest

from voidx.agent.domain.chat_policy import ChatResourceScope, ChatToolView


def test_chat_without_workspace_binds_only_non_local_tools():
    view = ChatToolView.for_scope(ChatResourceScope())

    assert view.allows("websearch")
    assert view.allows("webfetch")
    assert view.allows("mcp")
    assert not view.allows("read")
    assert not view.allows("write")
    assert not view.allows("bash")


def test_chat_workspace_binds_read_only_tools_and_scope():
    workspace = Path("/tmp/project").resolve()
    view = ChatToolView.for_scope(ChatResourceScope(workspace=workspace))

    assert view.allows("read", path=workspace / "README.md")
    assert view.allows("find", path=workspace / "src")
    assert view.allows("search", path=workspace / "src")
    assert not view.allows("glob", path=workspace / "src")
    assert not view.allows("grep", path=workspace / "src")
    assert not view.allows("write", path=workspace / "README.md")
    assert not view.allows("read", path=workspace.parent / "secret.txt")
    assert not view.allows("bash", path=workspace / "README.md")


def test_chat_denial_never_requests_approval():
    view = ChatToolView.for_scope(ChatResourceScope())

    decision = view.check("write")

    assert decision.allowed is False
    assert decision.reason == "tool_not_bound"
    assert decision.requests_approval is False


@pytest.mark.parametrize("tool_id", ["bash", "powershell", "agent", "subagent"])
def test_chat_never_binds_escape_tools(tool_id):
    view = ChatToolView.for_scope(ChatResourceScope(workspace=Path("/tmp/project")))
    assert not view.allows(tool_id)


def test_chat_check_tool_call_rejects_out_of_scope_file_path(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    view = ChatToolView.for_scope(ChatResourceScope(workspace=workspace))

    assert not view.check_tool_call("read", {"file_path": str(tmp_path / "secret.txt")}).allowed
    assert view.check_tool_call("read", {"file_path": str(workspace / "a.py")}).allowed


def test_chat_check_tool_call_denies_mcp_call_but_allows_discovery():
    view = ChatToolView.for_scope(ChatResourceScope())

    assert view.check_tool_call("mcp", {"op": "list"}).allowed
    assert view.check_tool_call("mcp", {"op": "load", "server": "docs"}).allowed
    decision = view.check_tool_call(
        "mcp", {"op": "call", "server": "docs", "tool": "write_page"}
    )
    assert not decision.allowed
    assert decision.requests_approval is False


def test_chat_check_tool_call_extracts_nested_paths(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    view = ChatToolView.for_scope(ChatResourceScope(workspace=workspace))

    decision = view.check_tool_call(
        "manage",
        {"moves": [{"src": str(workspace / "a"), "dest": str(workspace / "b")}]},
    )
    assert not decision.allowed
    assert decision.requests_approval is False

    decision = view.check_tool_call("read", {"paths": [str(tmp_path / "secret")]})
    assert not decision.allowed
    assert decision.requests_approval is False
