"""Smoke tests for tool system — types, execution, error handling."""

from tests.tool_registry import build_registry
import asyncio
import json
import logging
import shlex
import os
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.application.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.interaction import (
    UserInteraction,
    UserResponse,
)
from voidx.tooling.builtin.file import (
    FileReadInput,
    WriteInput,
    FileReadTool,
)
from voidx.tooling.adapters.persistence.file_snapshot import save_file_version
import voidx.tooling.application.file_state as file_state
from voidx.tooling.builtin.file.search import FindInput, SearchInput
from voidx.tooling.builtin.shell.bash import BashInput
from voidx.agent.adapters.tools.subagent import AgentInput, AgentTool
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


class TestToolRegistry:
    """Registry knows all tools."""

    def test_registry_accepts_only_explicit_plugins(self):
        registry = ToolRegistry([])
        assert registry.ids() == []

        class Plugin:
            id = "fake"
            description = "fake tool"

            def parameters_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, args, ctx):
                return ToolResult(output="ok")

        registry.register_plugin(Plugin())
        assert registry.ids() == ["fake"]

    def test_all_tools_registered(self):
        r = build_registry()
        ids = r.ids()
        assert "read" in ids
        assert "manage" in ids
        assert "file" not in ids
        assert "write" in ids
        assert "replace" in ids
        assert "line" not in ids
        assert "edit" not in ids
        assert "insert" not in ids
        assert "delete" not in ids
        assert "find" in ids
        assert "search" in ids
        assert "git" in ids
        assert ("bash" if os.name != "nt" else "powershell") in ids
        assert "clarify" in ids
        assert "checkpoint" in ids
        assert "workflow" in ids
        assert "advance_workflow" not in ids
        assert "skill" in ids
        assert "lsp" in ids
        assert "lsp_format" in ids

    def test_serialize_definitions(self):
        r = build_registry()
        tools = r.serialize_definitions()
        assert len(tools) == len(r.ids())
        assert len(tools) >= 10
        names = [t["function"]["name"] for t in tools]
        assert "manage" in names
        assert "file" not in names
        assert "write" in names
        assert "replace" in names
        assert "line" not in names
        assert "edit" not in names
        # Execution-only tools stay in the catalog serialization; the resolver hides them.
        assert "git" in names
        assert "lsp_format" in names
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]

    def test_builtin_tools_are_strict_and_mcp_gateway_is_not(self):
        r = build_registry()
        tools = r.serialize_definitions()
        builtin = next(t for t in tools if t["function"]["name"] == "read")
        gateway = next(t for t in tools if t["function"]["name"] == "mcp")
        assert builtin["function"]["strict"] is True
        assert "strict" not in gateway["function"]

    def test_nested_tool_schemas_keep_defs_and_checkpoint_is_flat(self):
        r = build_registry()
        clarify = r.get_def("clarify").parameters
        checkpoint = r.get_def("checkpoint").parameters

        assert clarify["properties"]["options"]["items"]["type"] == "string"
        assert "$defs" not in checkpoint
        assert checkpoint["properties"]["steps"]["items"]["type"] == "string"
        assert "alternatives" not in checkpoint["properties"]
        assert "estimated_steps" not in checkpoint["properties"]

    def test_unknown_tool(self):
        r = build_registry()
        assert r.get("nonexistent") is None

    def test_filter_tools_retains_only_allowed_tools(self):
        r = build_registry()

        r.filter_tools({"read", "search"})

        assert set(r.ids()) == {"read", "search"}
        assert r.get("read") is not None
        assert r.get("file") is None
        names = [tool["function"]["name"] for tool in r.serialize_definitions()]
        assert names == ["read", "search"]
