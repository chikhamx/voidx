import json
import sys
from pathlib import Path


import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.goal_resolver import ResolverGoal, resolve_goal_for_turn
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.turn_runner import _turn_exchange_from_final_messages
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
    TodoRunItem,
    TodoRunState,
    TurnExchange,
    WorkflowRoute,
)
from voidx.config import Config
from voidx.llm.usage import UsageStats
from voidx.memory.session import create_session, delete_session, load_messages
from voidx.runtime.intent import TaskIntent
from voidx.ui.output.dock import BottomInputDock, set_dock


class StructuredModel:
    def __init__(self, result):
        self.result = result
        self.messages = None

    def with_structured_output(self, schema):
        assert schema is ResolverGoal
        return self

    async def ainvoke(self, messages):
        self.messages = messages
        return self.result


@pytest.mark.asyncio
async def test_goal_resolver_records_raw_response_usage():
    class StructuredUsageModel:
        def with_structured_output(self, schema, *, include_raw=False):
            assert schema is ResolverGoal
            assert include_raw is True
            return self

        async def ainvoke(self, _messages):
            return {
                "raw": AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "total_tokens": 7,
                    },
                ),
                "parsed": ResolverGoal(
                    intent="coding",
                    goal="Track resolver usage",
                    workflow=None,
                ),
                "parsing_error": None,
            }

    usage_stats = UsageStats()

    result = await resolve_goal_for_turn(
        model=StructuredUsageModel(),
        user_text="track resolver usage",
        interaction_mode="auto",
        task_state=TaskState(),
        usage_stats=usage_stats,
    )

    assert result.goal is not None
    assert result.goal.desc == "Track resolver usage"
    assert usage_stats.last_input_tokens == 5
    assert usage_stats.last_output_tokens == 2
    assert usage_stats.total_input_tokens == 5
    assert usage_stats.total_output_tokens == 2
    assert usage_stats.total_calls == 1


@pytest.mark.asyncio
async def test_goal_resolver_uses_structured_llm_result():
    task_state = TaskState(
        recent_exchanges=[
            TurnExchange(user_text="之前说继续", assistant_text="我已经完成了 review 检查。"),
        ],
    )
    model = StructuredModel(
        ResolverGoal(
            intent="coding",
            goal="Review the runtime task state file",
            workflow="review",
            kind_hint="review",
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 这个文件",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.intent.type == TaskIntent.CODING
    assert result.goal is not None
    assert result.goal.desc == "Review the runtime task state file"
    assert result.plan == PlanResolution(join="review", leave=None)
    assert model.messages is not None
    assert [type(message) for message in model.messages] == [
        SystemMessage,
        HumanMessage,
    ]
    assert "You are a goal resolver." in model.messages[0].content
    assert "## Field Rules" in model.messages[0].content
    assert "## Available Workflows" in model.messages[0].content
    request = model.messages[1].content
    assert "# Recent Conversation" in request
    assert "之前说继续" in request
    assert "我已经完成了 review 检查。" in request
    assert "# Current User Question" in request
    assert "review 这个文件" in request
    assert "## Return Fields" not in request
    assert "## ResolverGoal Schema" not in request
    assert "## Available Workflows" not in request
    assert request.rstrip().endswith("review 这个文件")
    assert "workflow_start" not in model.messages[0].content
    assert "next_workflow" not in model.messages[0].content
    assert "Do not choose brainstorm" not in model.messages[0].content
    assert "title_requested" not in model.messages[0].content
    assert all("title_requested" not in message.content for message in model.messages[1:])



def test_goal_resolver_prompt_has_strict_workflow_selection_rules():
    model = StructuredModel(ResolverGoal(intent="coding", goal="review code", workflow="review"))

    # Build messages through the public resolver path so the test covers the actual prompt.
    import asyncio

    result = asyncio.run(
        resolve_goal_for_turn(
            model=model,
            user_text="review一下代码实现",
            interaction_mode="auto",
            task_state=TaskState(),
            log_diagnostic=False,
        )
    )

    assert result.intent.type == TaskIntent.CODING
    prompt = model.messages[0].content
    assert "## Workflow Selection Rules" in prompt
    assert "workflow is null by default" in prompt
    assert "Only set workflow when this turn must enter a workflow gate" in prompt
    assert "Do not set workflow for read-only inspection" in prompt
    assert "If an active workflow already covers the request" in prompt
    assert "debug: Set only for actual bugs, crashes, failing tests, tracebacks, or unexpected behavior requiring root-cause investigation." in prompt
    assert "review: Set only when the user asks to review code, design, implementation, or changes." in prompt
    assert "verify: Set only when the user asks to prove something is passing, fixed, complete, or safe." in prompt


def test_goal_resolver_prompt_keeps_goal_as_stable_task_objective():
    model = StructuredModel(ResolverGoal(intent="coding", goal="fix runtime bugs", workflow="debug"))

    import asyncio

    asyncio.run(
        resolve_goal_for_turn(
            model=model,
            user_text="Fix three identified bugs: stale upgrade marker, incomplete voidx-cli import check, and silent headless fallback",
            interaction_mode="auto",
            task_state=TaskState(),
            log_diagnostic=False,
        )
    )

    prompt = model.messages[0].content
    assert "Stable overall objective for the current task" in prompt
    assert "Preserve material constraints" in prompt
    assert "omitting transient execution detail" in prompt
    assert "without explicit details" not in prompt
    assert "status bar" not in prompt


def test_goal_resolution_schema_excludes_removed_fields():
    properties = GoalResolution.model_json_schema()["properties"]

    assert set(properties) == {"intent", "goal", "plan"}
    assert "confirmed_approval" not in properties
    assert "title" not in properties
    assert "workflow_start" not in properties
    assert "workflow_end" not in properties
    assert "next_workflow" not in properties


def test_goal_spec_schema_excludes_type():
    properties = GoalSpec.model_json_schema()["properties"]

    assert set(properties) == {"desc"}


def test_resolver_goal_requires_goal():
    with pytest.raises(ValueError):
        ResolverGoal(intent="general", goal=None)

    with pytest.raises(ValueError):
        ResolverGoal(intent="general", goal="")

    with pytest.raises(ValueError):
        ResolverGoal(intent="general", goal="   ")


def test_resolver_goal_allows_goal_without_workflow():
    goal_only = ResolverGoal(intent="general", goal="chat about weather")
    assert goal_only.goal == "chat about weather"
    assert goal_only.workflow is None


def test_resolver_goal_allows_goal_with_workflow():
    paired = ResolverGoal(intent="coding", goal="fix bug", workflow="debug")
    assert paired.goal == "fix bug"
    assert paired.workflow == "debug"


def test_resolver_goal_rejects_workflow_without_goal():
    with pytest.raises(ValueError):
        ResolverGoal(intent="coding", goal=None, workflow="review")


def test_resolver_goal_rejects_unknown_workflow():
    with pytest.raises(ValueError):
        ResolverGoal(intent="coding", goal="do work", workflow="nonexistent")


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

    assert result.plan == PlanResolution(join="review", leave=None)
    assert result.goal is not None
    assert result.goal.desc == "current diff"


@pytest.mark.asyncio
async def test_goal_resolver_legacy_shape_sets_kind_hint_only_in_logs(tmp_path, monkeypatch):
    from voidx.logging import request_log

    monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)
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

    assert result.goal == GoalSpec(desc="current diff")
    assert not hasattr(result.goal, "type")
    assert result.plan == PlanResolution(join="review", leave=None)

    entries = [
        json.loads(line)
        for line in (tmp_path / "llm_requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    decision = next(entry for entry in entries if entry.get("event") == "goal_resolver_decision")
    assert decision["resolver_kind_hint"] == "review"


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

    assert result.plan == PlanResolution(join="design", leave=None)
    assert result.goal is not None
    assert result.goal.desc == "write workflow approval spec"


@pytest.mark.asyncio
async def test_goal_resolver_drops_unknown_plan_route():
    model = StructuredModel(
        ResolverGoal(
            intent="coding",
            goal="continue",
            workflow="review",
            kind_hint="feature",
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.plan == PlanResolution(join="review", leave=None)
    assert result.goal is not None


@pytest.mark.asyncio
async def test_goal_resolver_treats_short_continue_after_completed_todos_as_general():
    model = StructuredModel(
        ResolverGoal(
            intent="coding",
            goal="continue",
            workflow="review",
            kind_hint="review",
        )
    )
    task_state = TaskState(
        current_goal=GoalSpec(desc="Review git policy hardening"),
        todo_state=TodoRunState(
            summary="3/3 done · 0 active · 0 pending",
            total=3,
            done=3,
            active=0,
            pending=0,
            items=[
                TodoRunItem(id="policy", content="Review policy", status="done"),
                TodoRunItem(id="tool", content="Review tool", status="done"),
                TodoRunItem(id="tests", content="Run regression suite", status="done"),
            ],
        ),
        recent_exchanges=[
            TurnExchange(
                user_text="review git policy hardening",
                assistant_text="Review complete: no blocking issues found.",
            ),
        ],
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal == task_state.current_goal
    assert result.plan is None


@pytest.mark.asyncio
async def test_goal_resolver_accepts_verify_workflow_from_legacy_shape():
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

    assert result.plan == PlanResolution(join="verify", leave=None)
    assert result.goal == GoalSpec(desc="continue")


@pytest.mark.asyncio
async def test_goal_resolver_allows_verify_workflow():
    model = StructuredModel(
        ResolverGoal(
            intent="coding",
            goal="verify current changes",
            workflow="verify",
            kind_hint="feature",
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="verify it",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.goal == GoalSpec(desc="verify current changes")
    assert result.plan == PlanResolution(join="verify", leave=None)


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
    assert result.goal == GoalSpec(desc="implement login")
    assert result.plan == PlanResolution(join="tdd", leave=None)


@pytest.mark.asyncio
async def test_goal_resolver_goal_mode_keeps_current_goal():
    current_goal = GoalSpec(desc="clean up runtime state")
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
    assert result.goal is not None
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
                "intent": "coding",
                "goal": "帮我修一个 bug",
                "workflow": "debug",
                "kind_hint": "bugfix",
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
    assert "You are a goal resolver." in exchange["request"]["messages"][0]["content"]
    assert "## ResolverGoal Schema" not in exchange["request"]["messages"][-1]["content"]
    assert exchange["request"]["messages"][-1]["role"] == "human"
    assert "帮我修一个 bug" in exchange["request"]["messages"][-1]["content"]
    assert exchange["response"]["raw"]["kind_hint"] == "bugfix"
    assert exchange["response"]["raw"]["workflow"] == "debug"

@pytest.mark.asyncio
async def test_goal_resolver_uses_function_calling_for_deepseek_protocol():
    """DeepSeek protocol models should use method='function_calling' for with_structured_output."""
    from voidx.llm.service import DeepSeekChatOpenAI

    class FakeDeepSeekModel(DeepSeekChatOpenAI):
        """Real subclass — isinstance works. Use object.__new__ to skip init."""

        _structured_method: str | None = None
        _messages: list | None = None

        def with_structured_output(self, schema, method=None, **kwargs):
            FakeDeepSeekModel._structured_method = method
            return self

        async def ainvoke(self, messages):
            FakeDeepSeekModel._messages = messages
            return ResolverGoal(
                intent="coding",
                goal="fix a bug",
                workflow="debug",
                kind_hint="debug",
            )

    model = object.__new__(FakeDeepSeekModel)
    result = await resolve_goal_for_turn(
        model=model,
        user_text="帮我修一个 bug",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert FakeDeepSeekModel._structured_method == "function_calling"
    assert result.intent.type == TaskIntent.CODING
    assert result.goal is not None
    assert result.goal.desc == "fix a bug"
    assert result.plan == PlanResolution(join="debug", leave=None)


@pytest.mark.asyncio
async def test_goal_resolver_uses_json_mode_for_deepseek_with_reasoning():
    """DeepSeek protocol with active reasoning → json_mode (avoids tool_choice conflict)."""
    from voidx.llm.service import DeepSeekChatOpenAI

    class FakeDeepSeekReasoningModel(DeepSeekChatOpenAI):
        _structured_method: str | None = None
        _messages: list | None = None

        def with_structured_output(self, schema, method=None, **kwargs):
            FakeDeepSeekReasoningModel._structured_method = method
            return self

        async def ainvoke(self, messages):
            FakeDeepSeekReasoningModel._messages = messages
            return ResolverGoal(
                intent="coding",
                goal="review the diff",
                workflow="review",
                kind_hint="review",
            )

    model = object.__new__(FakeDeepSeekReasoningModel)
    # Simulate Kimi / Doubao / etc. with thinking type=enabled.
    # Use object.__setattr__ to bypass Pydantic's __setattr__ on a
    # partially-initialized instance.
    object.__setattr__(model, "extra_body", {"thinking": {"type": "enabled"}})
    assert model.has_active_reasoning is True

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 一下改动",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert FakeDeepSeekReasoningModel._structured_method == "json_mode"
    assert result.intent.type == TaskIntent.CODING
    assert result.goal is not None
    assert result.goal.desc == "review the diff"


@pytest.mark.asyncio
async def test_goal_resolver_uses_json_mode_for_deepseek_with_qwen_reasoning():
    """Qwen's enable_thinking: True also triggers json_mode."""
    from voidx.llm.service import DeepSeekChatOpenAI

    class FakeQwenModel(DeepSeekChatOpenAI):
        _structured_method: str | None = None

        def with_structured_output(self, schema, method=None, **kwargs):
            FakeQwenModel._structured_method = method
            return self

        async def ainvoke(self, messages):
            return ResolverGoal(intent="general", goal="hello chat", workflow=None, kind_hint=None)

    model = object.__new__(FakeQwenModel)
    object.__setattr__(model, "extra_body", {"enable_thinking": True})
    assert model.has_active_reasoning is True

    await resolve_goal_for_turn(
        model=model,
        user_text="hello",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert FakeQwenModel._structured_method == "json_mode"
