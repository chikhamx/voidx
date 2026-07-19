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
from voidx.tools.file import FileReadInput, FileReadTool
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
from voidx.runtime.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


class TestMakeInteractCallback:
    @pytest.mark.asyncio
    async def test_returns_none_when_app_is_none(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback
        assert _make_interact_callback(None) is None

    @pytest.mark.asyncio
    async def test_str_options_route_to_ask_text(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

        calls = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                calls.append(("choice", prompt, choices))
                return "choice"

            async def ask_text(self, prompt, **kwargs):
                calls.append(("text", prompt, kwargs))
                return "user answer"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=["a", "b"],
        ))
        assert response.value == "user answer"
        assert response.free_text is True
        assert [c[0] for c in calls] == ["text"]

    @pytest.mark.asyncio
    async def test_tuple_options_route_to_ask_choice(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return choices[1][1]

            async def ask_text(self, prompt, **kwargs):
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("Label A", "a", "desc a"), ("Label B", "b", "desc b")],
        ))
        assert response.value == "b"
        assert not response.cancelled
        assert not response.free_text

    @pytest.mark.asyncio
    async def test_tuple_options_emit_permission_prompt_events_when_event_bus_running(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback
        from voidx.ui.output.events import PermissionPromptCleared, PermissionPromptShown, ui_events

        seen_events = []

        class FakeConsumer:
            def handle(self, event):
                seen_events.append(event)

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return choices[0][1]

            async def ask_text(self, prompt, **kwargs):
                return "text"

        ui_events.start(FakeConsumer())
        try:
            callback = _make_interact_callback(FakeApp())
            response = await callback(UserInteraction(
                prompt="Read file outside workspace? /tmp/example.rs",
                options=[("Yes", "allow", "Allow this read once"), ("No", "deny", "Do not read this file")],
            ))
            await ui_events.drain()
        finally:
            await ui_events.stop()

        assert response.value == "allow"
        shown = next(event for event in seen_events if isinstance(event, PermissionPromptShown))
        assert shown.prompt == "Read file outside workspace? /tmp/example.rs"
        assert shown.choices[:2] == [
            ("Yes", "allow", "Allow this read once"),
            ("No", "deny", "Do not read this file"),
        ]
        assert shown.tools
        assert shown.tools[0].name == "read"
        assert shown.tools[0].pattern == "/tmp/example.rs"
        assert shown.tools[0].args == {"file_path": "/tmp/example.rs"}
        assert any(isinstance(event, PermissionPromptCleared) for event in seen_events)

    @pytest.mark.asyncio
    async def test_tuple_options_appends_other_choice(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

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
            options=[("Label A", "a", "desc a")],
        ))

        assert response.value == "a"
        last_label, last_value, last_desc = captured_choices[-1]
        assert last_label == "Other…"
        assert last_value.startswith("__voidx_choice_prompt_other")

    @pytest.mark.asyncio
    async def test_tuple_options_other_invokes_text_and_marks_free_text(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

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
            options=[("Label A", "a", "desc a")],
        ))

        assert response.value == "custom answer"
        assert response.free_text is True
        assert [call[0] for call in calls] == ["choice", "text"]

    @pytest.mark.asyncio
    async def test_tuple_options_other_sentinel_collision_preserves_real_option(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

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
            options=[("Other", "__voidx_choice_prompt_other__", "sentinel value")],
        ))

        assert response.value == "__voidx_choice_prompt_other__"
        assert response.free_text is False
        assert text_called is False
        last_label, last_value, last_desc = captured_choices[-1]
        assert last_value == "__voidx_choice_prompt_other___1"

    @pytest.mark.asyncio
    async def test_ask_text_without_options(self):
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

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
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return None

            async def ask_text(self, prompt, **kwargs):
                return None

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("Label A", "a", "desc a")],
        ))

        assert response.cancelled is True
        assert response.value == ""
