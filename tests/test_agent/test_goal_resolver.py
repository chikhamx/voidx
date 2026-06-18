import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.goal_resolver import resolve_goal_for_turn
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.turn_runner import _turn_exchange_from_final_messages
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
    TurnExchange,
    WorkflowRoute,
)
from voidx.config import Config
from voidx.memory.session import create_session, delete_session, load_messages
from voidx.runtime.intent import TaskIntent
from voidx.ui.output.dock import BottomInputDock, set_dock


class StructuredModel:
    def __init__(self, result):
        self.result = result
        self.messages = None

    def with_structured_output(self, schema):
        assert schema is GoalResolution
        return self

    async def ainvoke(self, messages):
        self.messages = messages
        return self.result


@pytest.mark.asyncio
async def test_goal_resolver_uses_structured_llm_result():
    task_state = TaskState(
        recent_exchanges=[
            TurnExchange(user_text="之前说继续", assistant_text="我已经完成了 review 检查。"),
        ],
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="review requested"),
            goal=GoalSpec(type=GoalType.REVIEW, desc="src/voidx/runtime/task_state.py"),
            plan=PlanResolution(join="review", leave=None),
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 这个文件",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.intent.type == TaskIntent.CODING
    assert result.intent.desc == "review requested"
    assert result.goal is not None
    assert result.goal.type == GoalType.REVIEW
    assert result.goal.desc == "src/voidx/runtime/task_state.py"
    assert result.plan == PlanResolution(join="review", leave=None)
    assert model.messages is not None
    assert [type(message) for message in model.messages] == [
        SystemMessage,
        HumanMessage,
        AIMessage,
        HumanMessage,
    ]
    assert "GoalResolution schema:" in model.messages[0].content
    assert "goal: null or {type:" in model.messages[0].content
    assert "plan: null or {join:" in model.messages[0].content
    assert "Available join values" in model.messages[0].content
    assert "workflow_start" not in model.messages[0].content
    assert "next_workflow" not in model.messages[0].content
    assert "Do not choose brainstorm" not in model.messages[0].content
    assert model.messages[1].content == "之前说继续"
    assert model.messages[2].content == "我已经完成了 review 检查。"
    assert model.messages[3].content == "review 这个文件"
    assert "title_requested" not in model.messages[0].content
    assert all("title_requested" not in message.content for message in model.messages[1:])


def test_goal_resolution_schema_excludes_removed_fields():
    properties = GoalResolution.model_json_schema()["properties"]

    assert set(properties) == {"intent", "goal", "plan"}
    assert "confirmed_approval" not in properties
    assert "title" not in properties
    assert "workflow_start" not in properties
    assert "workflow_end" not in properties
    assert "next_workflow" not in properties


def test_turn_exchange_records_only_terminal_ai_reply():
    exchange = _turn_exchange_from_final_messages(
        "review 这个文件",
        [
            AIMessage(content="old assistant reply"),
            ToolMessage(content="tool output", tool_call_id="call_1"),
        ],
    )

    assert exchange is None

    exchange = _turn_exchange_from_final_messages(
        "review 这个文件",
        [AIMessage(content="final assistant reply")],
    )

    assert exchange == TurnExchange(user_text="review 这个文件", assistant_text="final assistant reply")


@pytest.mark.asyncio
async def test_goal_resolver_propagates_review_only_route():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "review only"},
            "goal": {"type": "review", "desc": "current diff"},
            "plan": {"join": "review", "leave": "review"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 一下这个",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan == PlanResolution(join="review", leave="review")
    assert result.goal is not None
    assert result.goal.desc == "current diff"


@pytest.mark.asyncio
async def test_goal_resolver_defaults_review_route_leave_to_join():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "review only"},
            "goal": {"type": "review", "desc": "current diff"},
            "plan": {"join": "review"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 一下这个",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan == PlanResolution(join="review", leave=None)


@pytest.mark.asyncio
async def test_goal_resolver_defaults_write_route_leave_to_verify():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "implement spec"},
            "goal": {"type": "feature", "desc": "implement current spec"},
            "plan": {"join": "tdd"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="按这个 spec 实现",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan == PlanResolution(join="tdd", leave=None)


@pytest.mark.asyncio
async def test_goal_resolver_propagates_valid_plan_join():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "user requested spec"},
            "goal": {"type": "doc", "desc": "write workflow approval spec"},
            "plan": {"join": "design", "leave": "design"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="可以，先写一个 spec",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan == PlanResolution(join="design", leave="design")
    assert result.goal is not None
    assert result.goal.type == GoalType.DOC


@pytest.mark.asyncio
async def test_goal_resolver_drops_unknown_plan_route():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "bad workflow target"},
            "goal": {"type": "feature", "desc": "continue"},
            "plan": {"join": "nonexistent", "leave": "also-missing"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan is None
    assert result.goal is None


@pytest.mark.asyncio
async def test_goal_resolver_drops_non_entry_plan_join():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "bad workflow entry"},
            "goal": {"type": "feature", "desc": "continue"},
            "plan": {"join": "verify", "leave": "verify"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan is None
    assert result.goal is None


@pytest.mark.asyncio
async def test_goal_resolver_plan_mode_forces_design_goal():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "model saw implementation words"},
            "goal": {"type": "feature", "desc": "implement login"},
            "plan": {"join": "tdd", "leave": "verify"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="实现登录",
        interaction_mode="plan",
        task_state=TaskState(),
    )

    assert result.intent.type == TaskIntent.CODING
    assert result.goal == GoalSpec(type=GoalType.FEATURE, desc="implement login")
    assert result.plan == PlanResolution(join="tdd", leave="verify")


@pytest.mark.asyncio
async def test_goal_resolver_goal_mode_keeps_current_goal():
    current_goal = GoalSpec(type=GoalType.CHORE, desc="clean up runtime state")
    model = StructuredModel(
        {
            "intent": {"type": "general", "desc": "model was unsure"},
            "goal": None,
            "plan": None,
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="goal",
        task_state=TaskState(current_goal=current_goal),
    )

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal is None
    assert result.plan is None


@pytest.mark.asyncio
async def test_goal_resolver_falls_back_to_general_when_structured_output_fails():
    class BrokenModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("unsupported")

    result = await resolve_goal_for_turn(
        model=BrokenModel(),
        user_text="看看 runtime 状态",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal is None
    assert result.plan is None


@pytest.mark.asyncio
async def test_goal_resolver_logs_fallback_decision(tmp_path, monkeypatch):
    from voidx.logging import request_log

    monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

    class BrokenModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("unsupported")

    await resolve_goal_for_turn(
        model=BrokenModel(),
        user_text="修一下 workflow 状态栏",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    entry = json.loads((tmp_path / "llm_requests.jsonl").read_text(encoding="utf-8").strip())
    assert entry["event"] == "goal_resolver_decision"
    assert entry["intent"] == "general"
    assert entry["goal_type"] == ""
    assert entry["plan_join"] == ""
    assert entry["fallback_reason"] == "structured_output_error"
    assert entry["fallback_error_type"] == "RuntimeError"
    assert entry["active_workflows"] == []


@pytest.mark.asyncio
async def test_goal_resolver_logs_native_request_and_response(tmp_path, monkeypatch):
    from voidx.logging import request_log

    monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

    class RawResponseModel:
        def with_structured_output(self, _schema):
            return self

        async def ainvoke(self, _messages):
            return {
                "intent": {"type": "coding", "desc": "bug fix"},
                "goal": {"type": "bugfix", "desc": "帮我修一个 bug"},
                "plan": {"join": "debug", "leave": "verify"},
            }

    await resolve_goal_for_turn(
        model=RawResponseModel(),
        user_text="帮我修一个 bug",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    entries = [
        json.loads(line)
        for line in (tmp_path / "llm_requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    exchange = next(entry for entry in entries if entry.get("event") == "goal_resolver_exchange")
    assert exchange["request"]["messages"][0]["role"] == "system"
    assert "GoalResolution schema:" in exchange["request"]["messages"][0]["content"]
    assert exchange["request"]["messages"][-1] == {"role": "human", "content": "帮我修一个 bug"}
    assert exchange["response"]["raw"]["goal"]["type"] == "bugfix"
    assert exchange["response"]["raw"]["plan"]["join"] == "debug"


