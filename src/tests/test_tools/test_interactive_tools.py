"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReadTool, FileTool, WriteTool, FileReplaceTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tools.skills import SkillsTool
from voidx.tools.document import DocumentTool, DocumentInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
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
        result = await FileTool().execute(
            {"file_path": "app.py", "op": "create", "overwrite": True},
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
        result = await FileTool().execute(
            {"file_path": "created.py", "op": "create"},
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
        await FileTool().execute({"file_path": "app.py", "op": "create", "overwrite": True}, ctx)
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
            "agent": "voidx",
            "mode": "review",
            "task": "Review one changed file",
            "target": "src/voidx/tools/agent.py",
        }
        args.update(overrides)
        return args

    @pytest.mark.asyncio
    async def test_agent_tool_rejects_missing_target(self, tmp_path):
        calls: list[object] = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return "should not run"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        result = await tool.execute(
            self._agent_args(target=""),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert "target" in result.output
        assert calls == []

    @pytest.mark.asyncio
    async def test_agent_tool_rejects_implement_without_success_criteria(self, tmp_path):
        calls: list[object] = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return "should not run"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        result = await tool.execute(
            self._agent_args(
                mode="implement",
                task="Implement the agent mode contract",
                target="src/voidx/tools/agent.py",
            ),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["delegation_rejected"] is True
        assert "success_criteria" in result.output
        assert calls == []

    @pytest.mark.asyncio
    async def test_agent_tool_rejects_invalid_preset_for_mode(self, tmp_path):
        calls: list[object] = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return "should not run"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        result = await tool.execute(
            self._agent_args(result_preset="implementation"),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["delegation_rejected"] is True
        assert "result_preset" in result.output
        assert calls == []

    @pytest.mark.asyncio
    async def test_agent_tool_rejects_missing_internal_result_preset_without_crashing(self, tmp_path, monkeypatch):
        import voidx.tools.agent as agent_module

        calls: list[object] = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return "should not run"

        monkeypatch.delitem(agent_module._RESULT_PRESETS, "review")
        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        result = await tool.execute(
            self._agent_args(result_preset="review"),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["delegation_rejected"] is True
        assert "result_preset" in result.output
        assert calls == []

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

        result = await tool.execute(
            self._agent_args(),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.output == "child result"
        assert result.metadata["goal"] == {"desc": "review: src/voidx/tools/agent.py"}
        assert result.metadata["workflow_route"] == {"join": "review", "leave": "review"}
        assert result.metadata["result_schema"] == "review_result"
        assert "Target: src/voidx/tools/agent.py" in captured["description"]
        assert "Result contract:" not in captured["description"]
        assert captured["goal_resolution"].goal.desc == "review: src/voidx/tools/agent.py"
        assert captured["result"].schema_name == "review_result"
        assert "PASS|FAIL|NEEDS_CHANGE" in captured["result"].format

    @pytest.mark.asyncio
    async def test_agent_tool_normalizes_inspect_without_goal_map(self, tmp_path):
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

        result = await tool.execute(
            self._agent_args(
                mode="inspect",
                task="Inspect the runtime module",
                target="src/voidx/runtime",
            ),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.output == "child result"
        goal_resolution = captured["goal_resolution"]
        assert goal_resolution.goal.desc == "inspect: src/voidx/runtime"
        assert goal_resolution.plan.join == "review"
        assert goal_resolution.plan.leave == "review"
        assert captured["result"].schema_name == "inspection_result"

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

        result = await tool.execute(
            self._agent_args(),
            ToolContext(workspace=str(tmp_path)),
        )
        assert result.output == "child result"
        assert "model" not in result.metadata

    @pytest.mark.asyncio
    async def test_agent_tool_normalizes_feedback_review_goal_without_review_join_rejection(self, tmp_path):
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

        result = await tool.execute(
            self._agent_args(
                mode="feedback",
                task="Address the review feedback",
                target="review comment about agent routing",
                success_criteria="Return accepted/rejected status and verification notes.",
            ),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.output == "child result"
        goal_resolution = captured["goal_resolution"]
        assert goal_resolution.goal.desc == "feedback: review comment about agent routing"
        assert goal_resolution.plan.join == "feedback"
        assert goal_resolution.plan.leave == "verify"
        assert captured["result"].schema_name == "feedback_result"

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

        result = await tool.execute(
            self._agent_args(
                mode="implement",
                task="Implement the agent mode contract",
                target="src/voidx/tools/agent.py",
                success_criteria="Focused tests pass for the new agent input schema.",
            ),
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.output == "child result"
        goal_resolution = captured["goal_resolution"]
        assert goal_resolution.goal.desc == "implement: src/voidx/tools/agent.py"
        assert goal_resolution.plan.join == "tdd"
        assert goal_resolution.plan.leave == "verify"
        assert captured["result"].schema_name == "implementation_result"

    @pytest.mark.asyncio
    async def test_agent_tool_auto_result_preset_follows_mode(self, tmp_path):
        expected = {
            "inspect": "inspection_result",
            "review": "review_result",
            "debug": "debug_result",
            "plan": "plan_result",
            "implement": "implementation_result",
            "feedback": "feedback_result",
        }
        captured: list[str] = []

        async def runner(agent_def, description, goal_resolution, result):
            captured.append(result.schema_name)
            return "child result"

        tool = AgentTool(
            runner,
            agent_resolver=lambda name: type("Agent", (), {"name": name, "model": None})(),
            available_agents=["voidx"],
        )

        for mode, schema_name in expected.items():
            result = await tool.execute(
                self._agent_args(
                    mode=mode,
                    task=f"Run {mode} child agent task",
                    target=f"target/{mode}",
                    success_criteria="Return structured status and verification notes.",
                ),
                ToolContext(workspace=str(tmp_path)),
            )
            assert result.output == "child result"
            assert result.metadata["result_schema"] == schema_name

        assert captured == list(expected.values())
