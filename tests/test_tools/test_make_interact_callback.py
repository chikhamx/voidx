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



class TestMakeInteractCallback:
    @pytest.mark.asyncio
    async def test_returns_none_when_app_is_none(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback
        assert _make_interact_callback(None) is None

    @pytest.mark.asyncio
    async def test_ask_choice_with_options(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return choices[1][1]

            async def ask_text(self, prompt, **kwargs):
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc a"), ("B", "b", "desc b")],
        ))
        assert response.value == "b"
        assert not response.cancelled
        assert not response.free_text

    @pytest.mark.asyncio
    async def test_ask_choice_appends_other_option(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        captured_choices = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                captured_choices.extend(choices)
                return choices[0][1]

            async def ask_text(self, prompt, **kwargs):
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc a")],
        ))

        assert response.value == "a"
        assert captured_choices[-1][0] == "Other (type your answer)"
        assert captured_choices[-1][2] == ""

    @pytest.mark.asyncio
    async def test_ask_choice_other_invokes_text_and_marks_free_text(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        calls = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                calls.append(("choice", prompt, choices))
                return choices[-1][1]

            async def ask_text(self, prompt, **kwargs):
                calls.append(("text", prompt, kwargs))
                return "custom answer"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc a")],
        ))

        assert response.value == "custom answer"
        assert response.free_text is True
        assert [call[0] for call in calls] == ["choice", "text"]

    @pytest.mark.asyncio
    async def test_ask_choice_other_sentinel_collision_preserves_real_option(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        text_called = False
        captured_choices = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                captured_choices.extend(choices)
                return "__voidx_choice_prompt_other__"

            async def ask_text(self, prompt, **kwargs):
                nonlocal text_called
                text_called = True
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("Real Other", "__voidx_choice_prompt_other__", "desc")],
        ))

        assert response.value == "__voidx_choice_prompt_other__"
        assert response.free_text is False
        assert text_called is False
        assert captured_choices[-1][1] == "__voidx_choice_prompt_other___1"

    @pytest.mark.asyncio
    async def test_ask_text_without_options(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return "choice"

            async def ask_text(self, prompt, **kwargs):
                return "user input"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(prompt="Enter text:"))
        assert response.value == "user input"

    @pytest.mark.asyncio
    async def test_cancelled_when_app_returns_none(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return None

            async def ask_text(self, prompt, **kwargs):
                return None

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc")],
        ))
        assert response.cancelled is True
        assert response.value == ""


