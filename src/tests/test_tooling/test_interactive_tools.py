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

        assert "[completed]" in wait_result.output
        assert "Result:\nchild result" in wait_result.output
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
        assert "[completed]" in wait_result.output
        assert "Result:\nchild result" in wait_result.output
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

        assert "[completed]" in wait_result.output
        assert "Result:\nchild result" in wait_result.output
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
    assert "[running]" in result.output
    assert result.metadata["run_id"] in result.output
    assert "agent_control" not in result.output
    assert "agent_control" in (result.next_step_hint or "")
    assert result.display == ""

    wait_result = await AgentControlTool().execute(
        {
            "action": "wait",
            "run_id": result.metadata["run_id"],
            "wait": "standard",
        },
        ToolContext(
            workspace=str(tmp_path),
            session_id="session-1",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        ),
    )

    assert "[completed]" in wait_result.output
    assert "Result:\ngateway child result" in wait_result.output
    assert wait_result.metadata["wait_outcome"] in {
        "terminal_reached_during_wait",
        "already_terminal",
    }
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
        {"action": "wait", "run_id": spawned.metadata["run_id"], "wait": "standard"},
        ctx,
    )

    assert "[failed]" in failed.output
    assert "Error: provider schema rejected tool definitions" in failed.output
    assert failed.metadata["status"] == "failed"
    assert failed.metadata["run"]["error"] == "provider schema rejected tool definitions"

@pytest.mark.asyncio
async def test_agent_tool_wait_timeout_returns_running_without_error(tmp_path, monkeypatch):
    import voidx.agent.adapters.tools.subagent_control as control_module

    monkeypatch.setitem(control_module._WAIT_TIMEOUTS, "standard", 0.01)
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
            "wait": "standard",
        },
        ctx,
    )

    assert wait_result.metadata.get("error") is not True
    assert wait_result.metadata["status"] == "running"
    assert "[running]" in wait_result.output
    release.set()
    await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"], "wait": "standard"},
        ctx,
    )


@pytest.mark.asyncio
async def test_agent_tool_default_wait_is_finite(tmp_path, monkeypatch):
    import voidx.agent.adapters.tools.subagent_control as control_module

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
            "goal": "Review finite wait handling",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/agent.py",
        },
        ctx,
    )
    monkeypatch.setitem(control_module._WAIT_TIMEOUTS, "standard", 0.01)

    wait_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"]},
        ctx,
    )

    assert wait_result.metadata["wait_outcome"] == "timed_out_still_running"
    assert wait_result.metadata["status"] == "running"
    release.set()
    await gateway.wait(
        requester_run_id=root_id,
        target_run_id=spawn_result.metadata["run_id"],
        timeout=1,
    )


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


@pytest.mark.asyncio
async def test_agent_spawn_result_uses_stable_display_name_contract(tmp_path):
    from voidx.agent.domain.subagent_display import subagent_display_name

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("spawn-contract-session")

    async def runner(*args, **kwargs):
        return "done"

    result = await AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name})(),
        available_agents=["voidx"],
    ).execute(
        {
            "mode": "review",
            "goal": "Review spawn contract",
            "detail": "Review the complete spawn result contract.",
            "scope": "src/voidx/agent/adapters/tools/subagent.py",
        },
        ToolContext(
            workspace=str(tmp_path),
            session_id="spawn-contract-session",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        ),
    )

    run_id = result.metadata["run_id"]
    display_name = subagent_display_name(run_id)
    assert result.output == f"{display_name} [running]\nrun_id: {run_id}"
    assert result.title == f"{display_name}: Review spawn contract"
    assert result.summary == f"{display_name} spawned"
    assert result.display == ""
    assert result.metadata == {"agent": "voidx", "run_id": run_id, "status": "running"}
    assert result.next_step_hint == (
        "Use agent_control(action='wait', wait='standard') when the result is needed, "
        "or continue with other independent work."
    )
    assert run_id not in result.next_step_hint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_factory", "args", "runtime", "expected_hint"),
    [
        (
            lambda: AgentTool(None, agent_resolver=None),
            {"mode": "review"},
            AgentToolRuntime(),
            "Correct the arguments before retrying.",
        ),
        (
            lambda: AgentTool(None, agent_resolver=None),
            {
                "mode": "review",
                "goal": " ",
                "detail": "A sufficiently complete execution brief.",
                "scope": "",
            },
            AgentToolRuntime(),
            "Correct the arguments before retrying.",
        ),
        (
            lambda: AgentTool(None, agent_resolver=None),
            {
                "mode": "review",
                "goal": "Review unavailable resolver",
                "detail": "A sufficiently complete execution brief.",
                "scope": "",
            },
            AgentToolRuntime(),
            "Restore child-agent execution availability before retrying.",
        ),
        (
            lambda: AgentTool(None, agent_resolver=lambda name: None),
            {
                "mode": "review",
                "goal": "Review unknown runner",
                "detail": "A sufficiently complete execution brief.",
                "scope": "",
            },
            AgentToolRuntime(),
            "Restore child-agent execution availability before retrying.",
        ),
        (
            lambda: AgentTool(
                lambda *args, **kwargs: None,
                agent_resolver=lambda name: type("Agent", (), {"name": name})(),
            ),
            {
                "mode": "review",
                "goal": "Review unavailable gateway",
                "detail": "A sufficiently complete execution brief.",
                "scope": "",
            },
            AgentToolRuntime(),
            "Restore agent gateway availability before retrying.",
        ),
    ],
)
async def test_agent_spawn_errors_include_specific_recovery_hints(
    tmp_path, tool_factory, args, runtime, expected_hint
):
    result = await tool_factory().execute(
        args,
        ToolContext(workspace=str(tmp_path), session_id="error-session", runtime=runtime),
    )

    assert result.metadata["error"] is True
    assert result.next_step_hint == expected_hint


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [TimeoutError("slow spawn"), RuntimeError("broken spawn")])
async def test_agent_spawn_runtime_errors_include_inspection_hint(tmp_path, exception):
    class FailingGateway:
        async def spawn(self, **kwargs):
            raise exception

    async def runner(*args, **kwargs):
        return "unused"

    result = await AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name})(),
    ).execute(
        {
            "mode": "review",
            "goal": "Review failed spawn",
            "detail": "A sufficiently complete execution brief.",
            "scope": "",
        },
        ToolContext(
            workspace=str(tmp_path),
            session_id="error-session",
            runtime=AgentToolRuntime(subagent_transport=FailingGateway(), run_id="root"),
        ),
    )

    assert result.metadata["error"] is True
    assert result.next_step_hint == "Inspect the error before starting a replacement run."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "expected_wait"),
    [
        (None, "maximum"),
        (0, "maximum"),
        (0.1, "standard"),
        (64, "standard"),
        (64.1, "extended"),
        (128, "extended"),
        (128.1, "maximum"),
    ],
)
async def test_agent_legacy_timeout_maps_to_finite_wait_tiers(
    tmp_path, monkeypatch, timeout, expected_wait
):
    captured = {}

    class CapturingControlTool:
        async def execute(self, args, ctx):
            captured.update(args)
            return ToolResult(output="captured")

    monkeypatch.setattr("voidx.agent.adapters.tools.subagent.AgentControlTool", CapturingControlTool)
    args = {"action": "wait", "target_run_id": "run_legacy"}
    if timeout is not None:
        args["timeout"] = timeout

    result = await AgentTool().execute(args, ToolContext(workspace=str(tmp_path)))

    assert result.output == "captured"
    assert captured == {"action": "wait", "run_id": "run_legacy", "wait": expected_wait}


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [-1, "not-a-number"])
async def test_agent_legacy_timeout_rejects_invalid_values_without_raising(tmp_path, timeout):
    result = await AgentTool().execute(
        {"action": "wait", "target_run_id": "run_legacy", "timeout": timeout},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata == {"error": True, "validation_error": True}
    assert result.next_step_hint == "Correct the arguments before retrying."
