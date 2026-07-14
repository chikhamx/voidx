"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReadTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.bash.tool import BashTool
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


@pytest.mark.skipif(os.name == "nt", reason="bash tool is not registered on Windows")
class TestBash:
    """Bash commands execute and capture output."""

    @pytest.mark.asyncio
    async def test_bash_echo(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("bash", {"command": "echo hello"}, ctx)
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["exit_code"] == 0
        assert "hello" in data["stdout"]
        assert "hello" in result.display
        assert result.summary == ""
        assert result.metadata["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_bash_blocks_workspace_escape_in_tool_layer(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        ctx = ToolContext(workspace=str(workspace))
        r = ToolRegistry()

        result = await r.execute_tool(
            "bash",
            {"command": f"printf nope > {shlex.quote(str(outside))}"},
            ctx,
        )

        assert result.metadata["blocked"] is True
        assert not outside.exists()

    @pytest.mark.asyncio
    async def test_bash_timeout_terminates_process(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path), permission_preset="full_access")
        r = ToolRegistry()

        sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(2)"'
        result = await r.execute_tool(
            "bash",
            {"command": f"{sleep_cmd}; printf late > late.txt", "timeout": 1},
            ctx,
        )
        await asyncio.sleep(2.2)

        assert result.metadata["timeout"] is True
        assert result.metadata["error"] is True
        assert result.metadata["error_kind"] == "tool_timeout"
        assert result.metadata["timeout_source"] == "shell"
        assert result.metadata["exit_code"] == -1
        # On Unix, killpg terminates the entire process group so late.txt is never created.
        # On Windows, process-group killing is unavailable, so the second command may still run.
        if sys.platform != "win32":
            assert not (tmp_path / "late.txt").exists()

    @pytest.mark.asyncio
    async def test_bash_git_hint_metadata_without_registry(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("bash", {"command": "git status"}, ctx)
        hint = result.metadata["route_hint"]
        assert hint["tool_id"] == "git"
        assert hint["command"] == "git status"
        assert result.metadata["skipped"] is True

    @pytest.mark.asyncio
    async def test_bash_auto_routes_git_when_registry_available(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path), tool_registry=ToolRegistry())
        result = await ctx.tool_registry.execute_tool("bash", {"command": "git status --porcelain"}, ctx)

        assert result.metadata.get("route_hint") is None
        assert result.metadata["tool"] == "git"
        assert result.metadata["routed_command"] == "git status --porcelain"
        assert result.metadata["routed_tool_args"] == {"args": "status --porcelain"}
        assert result.metadata["routed_from"] == "bash"

    @pytest.mark.asyncio
    async def test_bash_git_filtered_registry_falls_back_to_hint(self, tmp_path):
        registry = ToolRegistry().filtered_copy({"bash"})
        ctx = ToolContext(workspace=str(tmp_path), tool_registry=registry)
        result = await registry.execute_tool("bash", {"command": "git status"}, ctx)

        assert result.metadata["skipped"] is True
        assert result.metadata["route_hint"]["tool_id"] == "git"

    @pytest.mark.asyncio
    async def test_bash_git_config_global_option_falls_back_to_hint(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path), tool_registry=ToolRegistry())
        result = await ctx.tool_registry.execute_tool(
            "bash",
            {"command": "git -c core.quotePath=false status"},
            ctx,
        )

        assert result.metadata["skipped"] is True
        assert result.metadata["route_hint"]["tool_id"] == "git"

    @pytest.mark.asyncio
    async def test_bash_auto_route_git_reset_hard_still_denied_by_git_tool(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "voidx@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "VoidX Tests"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (repo / "f.txt").write_text("changed\n", encoding="utf-8")

        registry = ToolRegistry()
        command = "git reset --hard HEAD"
        ctx = ToolContext(
            workspace=str(repo),
            tool_registry=registry,
            permission_preset="full_access",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "dangerous"}],
        )
        result = await registry.execute_tool("bash", {"command": command}, ctx)
        payload = json.loads(result.output)

        assert result.metadata.get("route_hint") is None
        assert result.metadata["tool"] == "git"
        assert result.metadata["routed_from"] == "bash"
        assert payload["ok"] is False
        assert payload["error"].startswith("command_denied")
        assert (repo / "f.txt").read_text(encoding="utf-8") == "changed\n"

    @pytest.mark.asyncio
    async def test_bash_auto_route_git_push_still_uses_git_tool(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "voidx@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "VoidX Tests"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        registry = ToolRegistry()
        command = "git push origin HEAD"
        ctx = ToolContext(
            workspace=str(repo),
            tool_registry=registry,
            permission_preset="full_access",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "dangerous"}],
        )
        result = await registry.execute_tool("bash", {"command": command}, ctx)
        payload = json.loads(result.output)

        assert result.metadata.get("route_hint") is None
        assert result.metadata["tool"] == "git"
        assert result.metadata["routed_tool_args"] == {"args": "push origin HEAD"}
        assert payload["command"] == "push"
        assert payload["ok"] is False
        assert "git_policy_denied" not in payload["error"]
        assert "origin" in payload["error"]

    @pytest.mark.asyncio
    async def test_bash_route_hint_skips_execution(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("bash", {"command": "echo 'hello' > out.txt"}, ctx)

        assert not (tmp_path / "out.txt").exists()
        assert result.metadata["skipped"] is True
        assert result.metadata["route_hint"]["tool_id"] == "manage"
        assert result.next_step_hint

    @pytest.mark.asyncio
    async def test_bash_no_route_hint_for_complex_commands(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("bash", {"command": "ls -la"}, ctx)
        assert "route_hint" not in result.metadata


    @pytest.mark.asyncio
    async def test_bash_blocked_sets_error_metadata(self, tmp_path):
        """E1: blocked paths must set metadata['error'] = True for consistency."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        ctx = ToolContext(workspace=str(workspace))
        r = ToolRegistry()

        result = await r.execute_tool(
            "bash",
            {"command": f"printf nope > {shlex.quote(str(outside))}"},
            ctx,
        )
        assert result.metadata["blocked"] is True
        assert result.metadata["error"] is True

    @pytest.mark.asyncio
    async def test_bash_timeout_sets_error_metadata(self, tmp_path):
        """E1: timeout must set metadata['error'] = True for consistency."""
        ctx = ToolContext(workspace=str(tmp_path), permission_preset="full_access")
        r = ToolRegistry()

        sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(1.5)"'
        result = await r.execute_tool(
            "bash",
            {"command": sleep_cmd, "timeout": 1},
            ctx,
        )
        assert result.metadata["timeout"] is True
        assert result.metadata["error"] is True

    @pytest.mark.asyncio
    async def test_bash_nonzero_exit_sets_error_metadata(self, tmp_path):
        """E1: non-zero exit code must set metadata['error'] = True for consistency."""
        ctx = ToolContext(workspace=str(tmp_path), permission_preset="full_access")
        r = ToolRegistry()

        result = await r.execute_tool(
            "bash",
            {"command": "exit 1"},
            ctx,
        )
        assert result.metadata["exit_code"] == 1
        assert result.metadata["error"] is True
        assert result.summary == "exit 1"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="bash cancellation test requires Unix process groups")
async def test_bash_cancellation_terminates_process_tree(tmp_path):
    marker = tmp_path / "cancelled-late.txt"
    script = (
        "import time, pathlib; "
        "time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('late')"
    )
    task = asyncio.create_task(
        BashTool().execute(
            {"command": f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'},
            ToolContext(workspace=str(tmp_path), permission_preset="full_access"),
        )
    )

    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(1.1)
    assert not marker.exists()
