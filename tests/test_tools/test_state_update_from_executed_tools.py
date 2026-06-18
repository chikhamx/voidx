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



class TestStateUpdateFromExecutedTools:
    def test_merges_state_patches(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        patch1 = ToolStatePatch(intent=IntentResolution(type=TaskIntent.CODING, desc="clarify"))
        patch2 = ToolStatePatch(
            goal=GoalSpec(type=GoalType.FEATURE, desc="Refactor auth"),
            plan=PlanResolution(join="tdd", leave="verify"),
        )

        msg1 = ToolMessage(content="result1", tool_call_id="c1")
        msg2 = ToolMessage(content="result2", tool_call_id="c2")

        result1 = ToolResult(output="r1", metadata={"state_patch": patch1.model_dump(mode="json", exclude_unset=True)})
        result2 = ToolResult(output="r2", metadata={"state_patch": patch2.model_dump(mode="json", exclude_unset=True)})

        executed = [
            _ExecutedTool(message=msg1, result=result1, tool_call={"name": "clarify"}),
            _ExecutedTool(message=msg2, result=result2, tool_call={"name": "clarify"}),
        ]

        update = _state_update_from_executed_tools(executed)
        assert update["task_intent"] == "coding"
        assert update["current_goal"]["desc"] == "Refactor auth"
        assert update["current_goal"]["type"] == "feature"
        assert update["workflow_route"] == {"join": "tdd", "leave": "verify"}

    def test_later_patch_overrides_earlier(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        patch1 = ToolStatePatch(intent=IntentResolution(type=TaskIntent.GENERAL, desc="clarify"))
        patch2 = ToolStatePatch(intent=IntentResolution(type=TaskIntent.CODING, desc="clarify"))

        msg1 = ToolMessage(content="r1", tool_call_id="c1")
        msg2 = ToolMessage(content="r2", tool_call_id="c2")

        result1 = ToolResult(output="r1", metadata={"state_patch": patch1.model_dump(mode="json", exclude_unset=True)})
        result2 = ToolResult(output="r2", metadata={"state_patch": patch2.model_dump(mode="json", exclude_unset=True)})

        executed = [
            _ExecutedTool(message=msg1, result=result1, tool_call={"name": "clarify"}),
            _ExecutedTool(message=msg2, result=result2, tool_call={"name": "clarify"}),
        ]

        update = _state_update_from_executed_tools(executed)
        assert update["task_intent"] == "coding"

    def test_state_patch_updates_runtime_persona(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        patch = ToolStatePatch(persona="implement")
        msg = ToolMessage(content="r", tool_call_id="c1")
        result = ToolResult(
            output="r",
            metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
        )

        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "workflow"})]

        update = _state_update_from_executed_tools(executed)

        assert update["persona"] == "implement"

    def test_workflow_runs_merge_with_current_state(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(name="tdd", reason="existing"),
        ]
        patch = ToolStatePatch(workflow_runs=[
            WorkflowRunState(name="verify", reason="new"),
        ])
        msg = ToolMessage(content="r", tool_call_id="c1")
        result = ToolResult(
            output="r",
            metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
        )

        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "clarify"})]
        update = _state_update_from_executed_tools(executed, current_workflow_runs=current)

        assert [run.name for run in update["workflow_runs"]] == [
            "tdd",
            "verify",
        ]

    def test_no_patch_returns_empty(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        msg = ToolMessage(content="r", tool_call_id="c1")
        result = ToolResult(output="r", metadata={})
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "read"})]
        update = _state_update_from_executed_tools(executed)
        assert update == {}

    def test_auto_advance_review_has_issues(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        msg = ToolMessage(content="review result", tool_call_id="c1")
        result = ToolResult(
            output="verdict: FAIL\n\n## Issues\n- bug found",
            metadata={"agent": "review"},
        )
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "agent"})]
        update = _state_update_from_executed_tools(executed, current_workflow_runs=current)
        assert "workflow_runs" in update
        by_name = {r.name: r for r in update["workflow_runs"]}
        assert by_name["review"].status == WorkflowRunStatus.SATISFIED
        assert "feedback" in by_name
        assert by_name["feedback"].status == WorkflowRunStatus.ACTIVE

    def test_auto_advance_route_terminal_updates_turn(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
                updated_turn=3,
            ),
        ]
        msg = ToolMessage(content="review result", tool_call_id="c1")
        result = ToolResult(
            output="verdict: FAIL\n\n## Issues\n- bug found",
            metadata={"agent": "review"},
        )
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "agent"})]
        update = _state_update_from_executed_tools(
            executed,
            current_workflow_runs=current,
            current_workflow_route={"join": "review", "leave": "review"},
            turn_count=9,
        )

        by_name = {r.name: r for r in update["workflow_runs"]}
        assert by_name["review"].status == WorkflowRunStatus.SATISFIED
        assert by_name["review"].updated_turn == 9

    def test_auto_advance_failed_implementation(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        msg = ToolMessage(content="test output", tool_call_id="c1")
        result = ToolResult(
            output="1 failed, 2 passed",
            metadata={"exit_code": 1, "command": "pytest tests/"},
        )
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "bash"})]
        update = _state_update_from_executed_tools(executed, current_workflow_runs=current)
        assert "workflow_runs" in update
        by_name = {r.name: r for r in update["workflow_runs"]}
        assert by_name["verify"].status == WorkflowRunStatus.SATISFIED
        assert "tdd" in by_name
        assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE

    def test_auto_advance_failed_implementation_without_route_stops_generically(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        msg = ToolMessage(content="test output", tool_call_id="c1")
        result = ToolResult(
            output="1 failed, 2 passed",
            metadata={"exit_code": 1, "command": "pytest tests/"},
        )
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "bash"})]
        update = _state_update_from_executed_tools(executed, current_workflow_runs=current)

        assert update["should_continue"] is False

    def test_auto_advance_failed_implementation_can_loop_back_to_route_end(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        msg = ToolMessage(content="test output", tool_call_id="c1")
        result = ToolResult(
            output="1 failed, 2 passed",
            metadata={"exit_code": 1, "command": "pytest tests/"},
        )
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "bash"})]
        update = _state_update_from_executed_tools(
            executed,
            current_workflow_runs=current,
            current_workflow_route={"join": "tdd", "leave": "verify"},
        )

        by_name = {r.name: r for r in update["workflow_runs"]}
        assert by_name["verify"].status == WorkflowRunStatus.SATISFIED
        assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE
        assert update.get("should_continue", True) is True

    def test_auto_advance_skipped_when_node_already_satisfied(self):
        from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool

        current = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.SATISFIED,
            ),
        ]
        msg = ToolMessage(content="review result", tool_call_id="c1")
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "agent"})]
        update = _state_update_from_executed_tools(executed, current_workflow_runs=current)
        assert "workflow_runs" not in update


