"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import os
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import (
    FileReadInput,
    FileInput,
    WriteInput,
    FileReadTool,
)
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
from voidx.tools.document import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


class TestToolRegistry:
    """Registry knows all tools."""

    def test_all_tools_registered(self):
        r = ToolRegistry()
        ids = r.ids()
        assert "read" in ids
        assert "file" in ids
        assert "write" in ids
        assert "replace" in ids
        assert "line" not in ids
        assert "edit" not in ids
        assert "insert" not in ids
        assert "delete" not in ids
        assert "glob" in ids
        assert "grep" in ids
        assert "git" in ids
        assert ("bash" if os.name != "nt" else "powershell") in ids
        assert "clarify" in ids
        assert "checkpoint" in ids
        assert "workflow" in ids
        assert "advance_workflow" not in ids
        assert "skill" in ids
        assert "lsp" in ids
        assert "lsp_format" not in ids

    def test_tools_for_llm(self):
        r = ToolRegistry()
        tools = r.tools_for_llm()
        assert len(tools) == len(r.ids())
        assert len(tools) >= 10
        names = [t["function"]["name"] for t in tools]
        assert "file" in names
        assert "write" in names
        assert "replace" in names
        assert "line" not in names
        assert "edit" not in names
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]

    def test_builtin_tools_have_strict_mcp_tools_do_not(self):
        r = ToolRegistry()
        # Register a fake MCP tool
        r.register("mcp__tavily__search_abc12345", object(), "MCP search", {"type": "object", "properties": {}})
        tools = r.tools_for_llm()
        builtin = next(t for t in tools if t["function"]["name"] == "read")
        mcp = next(t for t in tools if t["function"]["name"].startswith("mcp__"))
        assert builtin["function"]["strict"] is True
        assert "strict" not in mcp["function"]

    def test_nested_tool_schemas_keep_defs_and_checkpoint_is_flat(self):
        r = ToolRegistry()
        clarify = r.get_def("clarify").parameters
        checkpoint = r.get_def("checkpoint").parameters

        assert clarify["properties"]["options"]["items"]["type"] == "string"
        assert "$defs" not in checkpoint
        assert checkpoint["properties"]["steps"]["items"]["type"] == "string"
        assert "alternatives" not in checkpoint["properties"]
        assert "estimated_steps" not in checkpoint["properties"]

    def test_unknown_tool(self):
        r = ToolRegistry()
        assert r.get("nonexistent") is None

    def test_filter_tools_retains_only_allowed_tools(self):
        r = ToolRegistry()

        r.filter_tools({"read", "grep"})

        assert set(r.ids()) == {"read", "grep"}
        assert r.get("read") is not None
        assert r.get("file") is None
        names = [tool["function"]["name"] for tool in r.tools_for_llm()]
        assert names == ["read", "grep"]
