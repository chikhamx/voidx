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
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReadTool, ManageTool, WriteTool, FileReplaceTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
from voidx.tools.search import FindInput, SearchInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.agent_control import AgentControlTool
from voidx.agent.gateway import AgentGateway
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tools.skills import SkillsTool
from voidx.tools.document import DocumentTool, DocumentInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.runtime.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.application.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


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
            "goal": "Review one changed file",
            "detail": "Execute this delegated task and report concrete findings.",
            "scope": "src/voidx/tools/agent.py",
        }
        args.update(overrides)
        return args

    async def _spawn_and_wait_agent(self, tool: AgentTool, args: dict, tmp_path):
        gateway = AgentGateway()
        root_id = gateway.ensure_root("session-1")
        ctx = ToolContext(
            workspace=str(tmp_path),
            session_id="session-1",
            agent_gateway=gateway,
            agent_run_id=root_id,
        )
        spawn_result = await tool.execute(args, ctx)
        assert spawn_result.metadata["run_id"]
        assert spawn_result.metadata["status"] == "running"
        wait_result = await AgentControlTool().execute(
            {
                "action": "wait",
                "run_id": spawn_result.metadata["run_id"],
                "wait": "brief",
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

        assert wait_result.output == "child result"
        assert spawn_result.metadata["goal"] == {"desc": "Review one changed file"}
        assert spawn_result.metadata["workflow_route"] == {"join": "review", "leave": "review"}
        assert spawn_result.metadata["result_schema"] == "review_result"
        assert "Scope: src/voidx/tools/agent.py" in captured["description"]
        assert "Result contract:" not in captured["description"]
        assert captured["goal_resolution"].goal.desc == "Review one changed file"
        assert captured["result"].schema_name == "review_result"
        assert "PASS|FAIL|NEEDS_CHANGE" in captured["result"].format
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

        assert wait_result.output == "child result"
        goal_resolution = captured["goal_resolution"]
        assert goal_resolution.goal.desc == "Review one changed file"
        assert goal_resolution.plan.join == "tdd"
        assert goal_resolution.plan.leave == "verify"
        assert captured["result"].schema_name == "implementation_result"
