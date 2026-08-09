"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.application.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from pydantic import Field, SkipValidation
from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.domain.result import ToolResult


class ToolContext(AgentToolExecutionContext):
    authorization_service: SkipValidation[AuthorizationRuntime] = Field(default_factory=AuthorizationRuntime)
    file_state: SkipValidation[FileStateStore] = Field(default_factory=FileStateStore)
    post_edit_formatter: SkipValidation[object | None] = None

from voidx.tooling.domain.interaction import (
    UserInteraction,
    UserResponse,
)
from voidx.tooling.builtin.file import FileReadInput, FileReadTool, ManageTool, WriteTool, FileReplaceTool
from voidx.tooling.adapters.persistence.file_snapshot import save_file_version
import voidx.tooling.application.file_state as file_state
from voidx.tooling.builtin.file.search import FindInput, SearchInput
from voidx.tooling.builtin.shell.bash import BashInput
from voidx.agent.adapters.tools.subagent import AgentInput, AgentTool
from voidx.agent.adapters.tools.subagent_control import AgentControlTool
from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.agent.adapters.tools.todo import TodoInput, TodoWriteTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.agent.adapters.tools.interaction.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tooling.adapters.skills import SkillsTool
from voidx.tooling.builtin.document import DocumentTool, DocumentInput
from voidx.agent.adapters.tools.interaction.checkpoint import PlanCheckpointTool
from voidx.agent.domain.task.state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.application.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.automation.workflow import WorkflowStateEventKind
import voidx.persistence.sqlite as store


class TestInteractiveTools:
    @pytest.mark.asyncio
    async def test_write_tool_saves_existing_file_version_before_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
        target = tmp_path / "app.py"
        target.write_text("old\n", encoding="utf-8")

        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")
        await FileReadTool().execute({"file_path": "app.py"}, ctx)
        result = await ManageTool().execute(
            {"op": "create", "paths": "app.py", "overwrite": True},
            ctx,
        )
        assert result.metadata.get("error") is not True
        await WriteTool().execute({"file_path": "app.py", "op": "append", "new_string": "new\n"}, ctx)

        history_dir = store.DATA_DIR / "sessions" / "sid-1" / "file-history"
        manifest_rows = [
            json.loads(line)
            for line in (history_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(manifest_rows) >= 1
        row = manifest_rows[0]
        assert row["path"] == "app.py"
        assert row["version"] == 1
        assert row["snapshot"].endswith("@v1")
        assert (history_dir / row["snapshot"]).read_text(encoding="utf-8") == "old\n"

    @pytest.mark.asyncio
    async def test_write_tool_saves_file_version_for_created_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")

        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")
        result = await ManageTool().execute(
            {"op": "create", "paths": "created.py"},
            ctx,
        )
        assert result.metadata.get("error") is not True
        await WriteTool().execute({"file_path": "created.py", "op": "append", "new_string": "hello\n"}, ctx)

        history_dir = store.DATA_DIR / "sessions" / "sid-1" / "file-history"
        assert history_dir.exists()
        rows = [
            json.loads(line)
            for line in (history_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 1
        assert rows[0]["path"] == "created.py"

    @pytest.mark.asyncio
    async def test_edit_tool_saves_next_file_version_before_edit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
        target = tmp_path / "app.py"
        target.write_text("one\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")

        await FileReadTool().execute({"file_path": "app.py"}, ctx)
        await ManageTool().execute({"op": "create", "paths": "app.py", "overwrite": True}, ctx)
        await WriteTool().execute({"file_path": "app.py", "op": "append", "new_string": "two\n"}, ctx)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)
        result = await FileReplaceTool().execute(
            {"file_path": "app.py", "bounds": [{"line_no": 1, "anchor": "two"}], "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        history_dir = store.DATA_DIR / "sessions" / "sid-1" / "file-history"
        rows = [
            json.loads(line)
            for line in (history_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [row["version"] for row in rows] == [1, 2, 3]
        assert (history_dir / rows[0]["snapshot"]).read_text(encoding="utf-8") == "one\n"
        assert (history_dir / rows[1]["snapshot"]).read_text(encoding="utf-8") == ""
        assert (history_dir / rows[2]["snapshot"]).read_text(encoding="utf-8") == "two\n"

    @pytest.mark.asyncio
    async def test_save_file_version_uses_full_hash_name_on_short_hash_collision(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
        first = tmp_path / "first.py"
        second = tmp_path / "second.py"
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")
        hashes = iter([
            "a" * 16 + "1" * 48,
            "a" * 16 + "2" * 48,
        ])

        class FakeHash:
            def __init__(self, value: str):
                self._value = value

            def hexdigest(self) -> str:
                return self._value

        monkeypatch.setattr(file_state.hashlib, "sha256", lambda _value: FakeHash(next(hashes)))
        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")

        await save_file_version(ctx, first, display_path="first.py", tool_name="edit")
        await save_file_version(ctx, second, display_path="second.py", tool_name="edit")

        history_dir = store.DATA_DIR / "sessions" / "sid-1" / "file-history"
        rows = [
            json.loads(line)
            for line in (history_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert rows[0]["snapshot"] == f"{'a' * 16}@v1"
        assert rows[1]["snapshot"] == f"{'a' * 16}{'2' * 48}@v1"
        assert (history_dir / rows[0]["snapshot"]).read_text(encoding="utf-8") == "first\n"
        assert (history_dir / rows[1]["snapshot"]).read_text(encoding="utf-8") == "second\n"

    def _agent_args(self, **overrides):
        args = {
            "mode": "review",
            "goal": "审查 agent 工具",
            "detail": "Review one changed file and report findings.",
            "scope": "src/voidx/tools/agent.py",
        }
        args.update(overrides)
        return args

    async def _spawn_and_wait_agent(self, tool: AgentTool, args: dict, tmp_path):
        gateway = InProcessSubagentGateway()
        root_id = gateway.ensure_root("session-1")
        ctx = ToolContext(
            workspace=str(tmp_path),
            session_id="session-1",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        )
        spawn_result = await tool.execute(args, ctx)
        assert spawn_result.metadata["run_id"]
        assert spawn_result.metadata["status"] == "running"
        from voidx.agent.adapters.tools.subagent_control import AgentControlTool
        wait_result = await AgentControlTool().execute(
            {
                "action": "wait",
                "run_id": spawn_result.metadata["run_id"],
                "wait": "extended",
            },
            ctx,
        )
        return spawn_result, wait_result
    @pytest.mark.asyncio
    async def test_agent_tool_normalizes_review_mode_to_goal_resolution_and_result_contract(self, tmp_path):
        captured: dict[str, object] = {}

        async def runner(agent_def, description, goal_resolution, result):
            captured.update({
                "agent": agent_def.name,
                "description": description,
                "goal_resolution": goal_resolution,
                "result": result,
            })
            return "child result"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        spawn_result, wait_result = await self._spawn_and_wait_agent(tool, self._agent_args(), tmp_path)

        assert "Agent run status: completed" in wait_result.output
        assert "Wait outcome: terminal_reached_during_wait" in wait_result.output
        assert "Final result:\nchild result" in wait_result.output
        assert set(spawn_result.metadata) == {"agent", "run_id", "status"}
        assert "Scope: src/voidx/tools/agent.py" in captured["description"]
        assert "Result contract:" not in captured["description"]
        assert captured["goal_resolution"].goal.desc == "审查 agent 工具"
        assert captured["result"].schema_name == "review_result"
        assert "PASS|FAIL|NEEDS_CHANGE" in captured["result"].format
    @pytest.mark.asyncio
    async def test_agent_tool_does_not_expose_model_param(self, tmp_path):
        captured: dict[str, object] = {}

        async def runner(agent_def, description, goal_resolution, result):
            captured["agent"] = agent_def.name
            return "child result"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        schema = tool.parameters_schema()
        assert "model" not in schema.get("properties", {})

        spawn_result, wait_result = await self._spawn_and_wait_agent(tool, self._agent_args(), tmp_path)
        assert "Agent run status: completed" in wait_result.output
        assert "Wait outcome: terminal_reached_during_wait" in wait_result.output
        assert "Final result:\nchild result" in wait_result.output
        assert "model" not in spawn_result.metadata
    @pytest.mark.asyncio
    async def test_agent_tool_normalizes_implement_mode_to_tdd_verify_route(self, tmp_path):
        captured: dict[str, object] = {}

        async def runner(agent_def, description, goal_resolution, result):
            captured.update({
                "goal_resolution": goal_resolution,
                "result": result,
            })
            return "child result"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        _spawn_result, wait_result = await self._spawn_and_wait_agent(
            tool,
            self._agent_args(
                mode="implement",
                task="Implement the agent mode contract",
                target="src/voidx/tools/agent.py",
                success_criteria="Focused tests pass for the new agent input schema.",
            ),
            tmp_path,
        )

        assert "Agent run status: completed" in wait_result.output
        assert "Wait outcome: terminal_reached_during_wait" in wait_result.output
        assert "Final result:\nchild result" in wait_result.output
        goal_resolution = captured["goal_resolution"]
        assert goal_resolution.goal.desc == "审查 agent 工具"
        assert goal_resolution.plan.join == "tdd"
        assert goal_resolution.plan.leave == "verify"
        assert captured["result"].schema_name == "implementation_result"



@pytest.mark.asyncio
async def test_agent_tool_spawn_uses_gateway_when_available(tmp_path):
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    captured: dict[str, object] = {}

    async def runner(agent_def, description, goal_resolution, result_contract, *, agent_run_id=None):
        captured.update({
            "agent_def": agent_def,
            "description": description,
            "agent_run_id": agent_run_id,
            "result_contract": result_contract,
        })
        return "gateway child result"

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
        available_agents=["voidx"],
    )

    result = await tool.execute(
        {
            "mode": "review",
            "goal": "Review the gateway result path",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/agent.py",
        },
        ToolContext(
            workspace=str(tmp_path),
            session_id="session-1",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        ),
    )

    assert result.metadata["run_id"]
    assert result.metadata["status"] == "running"
    assert "spawned" in result.output
    assert result.metadata["run_id"] in result.output
    assert "agent_control" not in result.output
    assert "agent_control" in (result.next_step_hint or "")
    assert result.display == ""

    wait_result = await AgentControlTool().execute(
        {
            "action": "wait",
            "run_id": result.metadata["run_id"],
            "wait": "brief",
        },
        ToolContext(
            workspace=str(tmp_path),
            session_id="session-1",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        ),
    )

    assert "Agent run status: completed" in wait_result.output
    assert "Final result:\ngateway child result" in wait_result.output
    assert wait_result.metadata["wait_outcome"] == "terminal_reached_during_wait"
    assert captured["agent_run_id"] == result.metadata["run_id"]
    assert gateway.get_run(
        requester_run_id=root_id,
        target_run_id=str(captured["agent_run_id"]),
    ).result == {"result": "gateway child result"}


@pytest.mark.asyncio
async def test_agent_tool_wait_returns_child_error(tmp_path):
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")

    async def runner(*args, **kwargs):
        raise RuntimeError("provider schema rejected tool definitions")

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
        available_agents=["voidx"],
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-1",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )
    spawned = await tool.execute(
        {
            "mode": "review",
            "goal": "Trigger a child failure",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/registry.py",
        },
        ctx,
    )

    failed = await AgentControlTool().execute(
        {"action": "wait", "run_id": spawned.metadata["run_id"], "wait": "brief"},
        ctx,
    )

    assert "Agent run status: failed" in failed.output
    assert "Final result:\nprovider schema rejected tool definitions" in failed.output
    assert "Wait outcome: terminal_reached_during_wait" in failed.output
    assert failed.metadata["status"] == "failed"
    assert failed.metadata["run"]["error"] == "provider schema rejected tool definitions"

@pytest.mark.asyncio
async def test_agent_tool_wait_timeout_returns_running_without_error(tmp_path):
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    release = asyncio.Event()

    async def runner(*args, **kwargs):
        await release.wait()
        return "late result"

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
        available_agents=["voidx"],
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-1",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )

    spawn_result = await tool.execute(
        {
            "mode": "review",
            "goal": "Review the timeout handling",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/agent.py",
        },
        ctx,
    )

    wait_result = await AgentControlTool().execute(
        {
            "action": "wait",
            "run_id": spawn_result.metadata["run_id"],
            "wait": "brief",
        },
        ctx,
    )

    assert wait_result.metadata.get("error") is not True
    assert wait_result.metadata["status"] == "running"
    assert wait_result.metadata["status"] == "running"
    assert "Agent run status: running" in wait_result.output
    assert "Wait outcome: timed_out_still_running" in wait_result.output
    assert "Terminal: false" in wait_result.output
    release.set()
    await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"], "wait": "brief"},
        ctx,
    )


@pytest.mark.asyncio
async def test_agent_tool_wait_timeout_zero_waits_until_terminal(tmp_path):
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    release = asyncio.Event()

    async def runner(*args, **kwargs):
        await release.wait()
        return "late result"

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
        available_agents=["voidx"],
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-1",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )

    spawn_result = await tool.execute(
        {
            "mode": "review",
            "goal": "Review the timeout handling",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/agent.py",
        },
        ctx,
    )

    async def wait_zero():
        return await AgentControlTool().execute(
            {"action": "wait", "run_id": spawn_result.metadata["run_id"], "wait": "until_complete"},
            ctx,
        )

    wait_task = asyncio.create_task(wait_zero())
    await asyncio.sleep(0.05)
    assert not wait_task.done()
    release.set()
    wait_result = await asyncio.wait_for(wait_task, timeout=1)
    assert "Agent run status: completed" in wait_result.output
    assert "Final result:\nlate result" in wait_result.output
    assert wait_result.metadata["wait_outcome"] == "terminal_reached_during_wait"
    assert wait_result.metadata["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_tool_spawn_requires_gateway(tmp_path):
    async def runner(*args, **kwargs):
        return "should not run"

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
        available_agents=["voidx"],
    )

    result = await tool.execute(
        {
            "mode": "review",
            "goal": "Review the timeout behavior in this module",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/agent.py",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata["error"] is True
    assert result.metadata["reason"] == "gateway_unavailable"
