"""Child tool surface must match the mode capability policy.

The LLM-visible surface and the executable registry are the same filtered
child registry: a tool the policy removes is neither visible nor callable,
even via a forged tool call.
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import AIMessage

from tests.tool_registry import build_registry
from voidx.agent.adapters.langgraph.runtime import subagent as subagent_module
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent
from voidx.agent.adapters.tools.subagent import AgentResultContract
from voidx.agent.application.agents import AgentDef
from voidx.agent.application.subagent_policy import (
    CHILD_BLOCKED_TOOL_IDS,
    child_allowed_tool_ids,
)

from voidx.agent.domain.task.state import (

    GoalResolution,
    GoalSpec,
    PlanResolution,
)
from voidx.config import Config
from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.domain.result import ToolResult

_SHELL_ID = "powershell" if os.name == "nt" else "bash"

_READ_ONLY_IDS = {"read", "find", "search", "lsp", "document", "websearch", "webfetch"}
_PARENT_ONLY_IDS = {"agent", "agent_control", "clarify", "checkpoint", "workflow"}


class _CapturingModel:
    def __init__(self) -> None:
        self.tool_names: list[str] = []

    def bind_tools(self, tool_defs):
        self.tool_names = [
            item.get("function", {}).get("name", "")
            for item in tool_defs
            if isinstance(item, dict)
        ]
        return self


class _FakeLspManager:
    def has_available_server(self):
        return True


class _FakeUi:
    def step_header(self, _persona):
        return None

    def print(self, _text=""):
        return None


class _FakeEvents:
    async def emit(self, _event):
        return None

    def emit_direct(self, _event):
        return None


class _FakeUiPort:
    ui = _FakeUi()
    events = _FakeEvents()
    console = object()

    def via_events(self):
        return False


def _agent_def() -> AgentDef:
    return AgentDef(
        name="voidx",
        description="test",
        when_to_use="test",
        can_write=True,
        can_delegate=False,
    )


def _goal_resolution(join: str) -> GoalResolution:
    return GoalResolution(
        goal=GoalSpec(desc="surface probe"),
        plan=PlanResolution(join=join, leave=join),
    )


def _contract() -> AgentResultContract:
    return AgentResultContract(format="verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions")


async def _capture_child_tool_names(tmp_path, monkeypatch, join: str) -> list[str]:
    model = _CapturingModel()

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await run_subagent(
        _agent_def(),
        "Probe the visible tool surface",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(join),
        result_contract=_contract(),
        parent_tools=build_registry(),
        lsp_manager=_FakeLspManager(),
        debug=False,
        ui_port=_FakeUiPort(),
    )
    assert output == "done"
    return model.tool_names


@pytest.mark.asyncio
async def test_review_child_surface_is_read_only(tmp_path, monkeypatch):
    names = set(await _capture_child_tool_names(tmp_path, monkeypatch, "review"))

    assert _READ_ONLY_IDS <= names
    assert "message" in names
    for forbidden in (
        _PARENT_ONLY_IDS
        | {"write", "replace", "manage", "todo", "skill", "mcp", "bash", "powershell"}
    ):
        assert forbidden not in names, f"{forbidden} must not be visible to review child"


@pytest.mark.asyncio
async def test_debug_child_surface_adds_shell_but_not_writes(tmp_path, monkeypatch):
    names = set(await _capture_child_tool_names(tmp_path, monkeypatch, "debug"))

    assert _READ_ONLY_IDS <= names
    assert _SHELL_ID in names
    for forbidden in _PARENT_ONLY_IDS | {"write", "replace", "manage", "todo", "skill", "mcp"}:
        assert forbidden not in names, f"{forbidden} must not be visible to debug child"


@pytest.mark.asyncio
async def test_implement_child_surface_keeps_write_tools(tmp_path, monkeypatch):
    names = set(await _capture_child_tool_names(tmp_path, monkeypatch, "tdd"))

    assert _READ_ONLY_IDS <= names
    for expected in {"write", "replace", "manage", "todo", _SHELL_ID, "message"}:
        assert expected in names
    for forbidden in _PARENT_ONLY_IDS | {"skill", "mcp"}:
        assert forbidden not in names


@pytest.mark.asyncio
async def test_forged_write_call_in_review_child_is_rejected(tmp_path, monkeypatch):
    """A tool call that bypasses the surface must fail at the registry layer."""
    calls = 0

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "write",
                    "args": {"file_path": str(tmp_path / "forged.txt"), "content": "x"},
                    "id": "forge-1",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await run_subagent(
        _agent_def(),
        "Attempt a forged write",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution("review"),
        result_contract=_contract(),
        parent_tools=build_registry(),
        lsp_manager=_FakeLspManager(),
        debug=False,
        ui_port=_FakeUiPort(),
    )

    assert output == "done"
    assert not (tmp_path / "forged.txt").exists()


def test_child_allowed_tool_ids_defaults_unknown_mode_to_read_only() -> None:
    available = {"read", "write", "bash", "agent", "agent_control", "todo", "message"}

    review = child_allowed_tool_ids("review", available)
    unknown = child_allowed_tool_ids("unknown-mode", available)

    assert review == unknown
    assert "write" not in review
    assert "agent_control" not in review
    assert CHILD_BLOCKED_TOOL_IDS.isdisjoint(review)


def test_child_allowed_tool_ids_never_includes_parent_only_tools() -> None:
    available = {"read", "agent", "agent_control", "clarify", "checkpoint", "workflow"}

    for mode in ("review", "debug", "implement"):
        allowed = child_allowed_tool_ids(mode, available)
        assert allowed == {"read"}


class _RecordingShellTool:
    id = "bash"
    description = "recording shell"

    def __init__(self) -> None:
        self.commands: list[str] = []

    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, args, ctx) -> ToolResult:
        self.commands.append(str(args.get("command", "")))
        return ToolResult(output="ok")


def _guarded_shell() -> tuple[object, _RecordingShellTool]:
    from voidx.tooling.adapters.scoped_plugin import ReadOnlyShellPlugin

    inner = _RecordingShellTool()
    plugin = ReadOnlyShellPlugin(
        inner,
        AuthorizationRuntime(),
        FileStateStore(),
    )
    return plugin, inner


@pytest.mark.asyncio
async def test_read_only_shell_guard_allows_safe_read_commands() -> None:
    plugin, inner = _guarded_shell()
    ctx = ToolExecutionContext(workspace="/tmp")

    for command in ("ls -la", "cat foo.py"):
        result = await plugin.execute({"command": command}, ctx)
        assert result.output == "ok", command

    assert inner.commands == ["ls -la", "cat foo.py"]


@pytest.mark.asyncio
async def test_read_only_shell_guard_rejects_write_and_opaque_commands() -> None:
    plugin, inner = _guarded_shell()
    ctx = ToolExecutionContext(workspace="/tmp")

    for command in (
        "echo hi > /tmp/x",
        "rm -rf /tmp/x",
        "mkdir /tmp/d",
        "pytest tests/ -x",
        "python exploit.py",
    ):
        result = await plugin.execute({"command": command}, ctx)
        assert result.metadata.get("error") is True, command
        assert result.metadata.get("reason") == "read_only_shell", command

    assert inner.commands == []
