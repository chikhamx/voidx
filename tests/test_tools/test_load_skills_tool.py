"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file_ops import (
    FileReadInput,
    FileWriteInput,
    FileEditInput,
    EditEntry,
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    _find_paragraph,
)
from voidx.tools.file_state import save_file_version
import voidx.tools.file_state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, ClarifyOption, _infer_state_patch
from voidx.tools.load_skills import LoadSkillsTool
from voidx.tools.load_doc_template import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.plan_checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, GoalType, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


def _replace(lineno: int, prefix: str, suffix: str | None = None, new_string: str = "") -> dict:
    return {
        "operation": "replace",
        "lineno": lineno,
        "prefix": prefix,
        "suffix": prefix if suffix is None else suffix,
        "new_string": new_string,
    }


def _insert(lineno: int, prefix: str, suffix: str | None = None, new_string: str = "") -> dict:
    return {
        "operation": "insert",
        "lineno": lineno,
        "prefix": prefix,
        "suffix": prefix if suffix is None else suffix,
        "new_string": new_string,
    }


def _insert_bof(new_string: str) -> dict:
    return {"operation": "insert", "lineno": 0, "prefix": "", "suffix": "", "new_string": new_string}



class TestLoadSkillsTool:
    def _write_skill(self, workspace: Path, dirname: str, text: str) -> None:
        skill_dir = workspace / ".voidx" / "skills" / dirname
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")

    @pytest.mark.asyncio
    async def test_loads_enabled_skill_by_name(self, tmp_path):
        self._write_skill(
            tmp_path,
            "docs",
            "---\nname: docs\ndescription: Write docs\n---\nDocs body",
        )

        result = await LoadSkillsTool().execute(
            {"names": ["docs"]},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["count"] == 1
        assert result.metadata["loaded_skills"][0]["scope"] == "project"
        assert "VOIDX_SKILL_TOOL_CONTEXT" in result.output
        assert "## Skill: docs" in result.output
        assert "Source: project" in result.output
        assert "Docs body" in result.output

    @pytest.mark.asyncio
    async def test_load_skills_still_uses_tool_context_marker(self, tmp_path):
        self._write_skill(
            tmp_path,
            "docs",
            "---\nname: docs\ndescription: Write docs\n---\nDocs body",
        )

        result = await LoadSkillsTool().execute(
            {"names": ["docs"]},
            ToolContext(workspace=str(tmp_path)),
        )

        assert SKILL_TOOL_CONTEXT_MARKER in result.output
        assert result.metadata["loaded_skills"][0]["name"] == "docs"

    @pytest.mark.asyncio
    async def test_rejects_path_input(self, tmp_path):
        result = await LoadSkillsTool().execute(
            {"names": [".voidx/skills/docs/SKILL.md"]},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["invalid"] == [".voidx/skills/docs/skill.md"]
        assert "Invalid skill names" in result.output

    @pytest.mark.asyncio
    async def test_reports_missing_and_disabled_skills(self, tmp_path):
        self._write_skill(
            tmp_path,
            "disabled",
            "---\nname: disabled\nenabled: false\n---\nDisabled body",
        )

        result = await LoadSkillsTool().execute(
            {"names": ["missing", "disabled"]},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["missing"] == ["missing"]
        assert result.metadata["disabled"] == ["disabled"]
        assert "Disabled body" not in result.output

    @pytest.mark.asyncio
    async def test_builtin_workflow_nodes_are_not_loaded_as_skills(self, tmp_path):
        blocked = await LoadSkillsTool().execute(
            {"names": ["debug"]},
            ToolContext(workspace=str(tmp_path)),
        )
        loaded = await LoadSkillsTool().execute(
            {"names": ["debug"], "include_bundled": True},
            ToolContext(workspace=str(tmp_path)),
        )

        assert blocked.metadata["error"] is True
        assert blocked.metadata["missing"] == ["debug"]
        assert blocked.metadata["bundled_blocked"] == []
        assert loaded.metadata["error"] is True
        assert loaded.metadata["missing"] == ["debug"]
        assert loaded.metadata["loaded_skills"] == []

    @pytest.mark.asyncio
    async def test_enforces_total_output_limit_by_truncating(self, tmp_path):
        self._write_skill(
            tmp_path,
            "large",
            "---\nname: large\ndescription: Large skill\n---\n" + ("x" * 30_000),
        )

        result = await LoadSkillsTool().execute(
            {"names": ["large"]},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["truncated"] is True
        assert len(result.output) < 25_000
        assert "output truncated" in result.output


