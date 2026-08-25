from types import SimpleNamespace

import pytest

from tests.agent_tool_context import agent_tool_context as ToolContext
from voidx.agent.adapters.tools.automation.goal import (
    GoalCheckpointTool,
    GoalDecisionTool,
    GoalInitTool,
)
from voidx.agent.adapters.tools.context import AgentToolRuntime
from voidx.agent.domain.automation.goal import GoalSpec
from voidx.tooling.domain.interaction import UserInteraction, UserResponse


class RecordingGoalStore:
    def __init__(self) -> None:
        self.records = []

    async def submit_goal_protocol(self, record, **kwargs):
        self.records.append(record)
        return record


class DecisionController:
    def __init__(self):
        self.calls = []

    async def submit_decision(self, decision, *, protocol_id=""):
        self.calls.append({"decision": decision, "protocol_id": protocol_id})
        return SimpleNamespace(outcome="completed", summary=decision["summary"])


class IntakeController:
    def __init__(self):
        self.spec = None
        self.cancelled = False

    async def submit_init(self, spec):
        self.spec = spec
        return spec

    def final_spec(self):
        return self.spec

    def cancel(self):
        self.cancelled = True


def _runtime(
    store=None,
    *,
    phase: str,
    intake=None,
    controller=None,
    attempt: int = 0,
    interaction=None,
) -> AgentToolRuntime:
    return AgentToolRuntime(
        goal_intake=intake,
        goal_control=controller,
        goal_phase=phase,
        goal_store=store,
        goal_generation="gen-1",
        goal_parent_session_id="parent-session",
        goal_main_session_id="main-session",
        goal_work_session_id="work-session",
        goal_evaluator_session_id="evaluator-session",
        goal_turn_id=f"turn-{phase}-{attempt}",
        goal_attempt_number=attempt,
        interaction=interaction,
    )


def _context(runtime: AgentToolRuntime, *, session_id: str = "parent-session"):
    return ToolContext(workspace="/tmp", session_id=session_id, runtime=runtime)


@pytest.mark.asyncio
async def test_goal_phase_tool_schemas_are_disjoint_and_typed() -> None:
    init_schema = GoalInitTool().parameters_schema()
    checkpoint_schema = GoalCheckpointTool().parameters_schema()
    decision_schema = GoalDecisionTool().parameters_schema()

    assert init_schema["required"] == ["objective", "acceptance_condition"]
    assert set(init_schema["properties"]) == {
        "objective", "acceptance_condition", "achievement_method", "max_attempts",
    }
    assert set(checkpoint_schema["properties"]) == {
        "summary", "evidence", "changed_files", "verification", "next_hint", "progress",
    }
    assert decision_schema["properties"]["status"]["enum"] == [
        "finished", "continue", "blocked",
    ]
    assert set(decision_schema["properties"]) == {
        "status", "summary", "evidence", "reason", "next_hint", "missing_evidence", "progress",
    }


@pytest.mark.asyncio
async def test_goal_init_rejects_missing_durable_binding() -> None:
    controller = IntakeController()
    runtime = _runtime(phase="intake", intake=controller)

    result = await GoalInitTool().execute(
        {"objective": "ship feature", "acceptance_condition": "tests pass"},
        _context(runtime),
    )

    assert result.metadata["error"] is True
    assert result.metadata["goal_init_submitted"] is False
    assert controller.final_spec() is None


@pytest.mark.asyncio
async def test_goal_init_submits_durable_record_before_controller() -> None:
    store = RecordingGoalStore()
    controller = IntakeController()
    runtime = _runtime(store, phase="intake", intake=controller)

    result = await GoalInitTool().execute(
        {
            "objective": "ship feature",
            "acceptance_condition": "tests pass",
            "achievement_method": "use TDD",
            "max_attempts": 12,
        },
        _context(runtime, session_id="main-session"),
    )

    assert result.metadata["goal_init_submitted"] is True
    assert isinstance(controller.final_spec(), GoalSpec)
    assert controller.final_spec().max_attempts == 12
    assert len(store.records) == 1
    assert store.records[0].phase == "init"
    assert store.records[0].sequence_number == 0


@pytest.mark.asyncio
async def test_goal_init_approval_revision_does_not_submit_record() -> None:
    store = RecordingGoalStore()
    controller = IntakeController()

    async def interact(request: UserInteraction) -> UserResponse:
        return UserResponse(value="tighten acceptance", free_text=True)

    runtime = _runtime(
        store,
        phase="intake",
        intake=controller,
        interaction=interact,
    )
    result = await GoalInitTool().execute(
        {"objective": "ship feature", "acceptance_condition": "tests pass"},
        _context(runtime, session_id="main-session"),
    )

    assert result.metadata["goal_init_submitted"] is False
    assert result.metadata["goal_init_decision"] == "revised"
    assert store.records == []
    assert controller.final_spec() is None


@pytest.mark.asyncio
async def test_goal_checkpoint_submits_typed_work_record() -> None:
    store = RecordingGoalStore()
    runtime = _runtime(store, phase="work", attempt=1)

    result = await GoalCheckpointTool().execute(
        {
            "summary": "implemented",
            "evidence": ["tests passed"],
            "changed_files": ["src/app.py"],
            "verification": ["./test.py --backend"],
            "progress": "meaningful",
        },
        _context(runtime, session_id="work-session"),
    )

    assert result.metadata["goal_checkpoint_submitted"] is True
    assert store.records[0].phase == "checkpoint"
    assert store.records[0].sequence_number == 1
    assert store.records[0].payload_model().summary == "implemented"


@pytest.mark.asyncio
async def test_goal_checkpoint_rejects_empty_summary_without_store_write() -> None:
    store = RecordingGoalStore()
    runtime = _runtime(store, phase="work", attempt=1)

    result = await GoalCheckpointTool().execute(
        {"summary": "   "},
        _context(runtime, session_id="work-session"),
    )

    assert result.metadata["error"] is True
    assert result.metadata["goal_checkpoint_submitted"] is False
    assert store.records == []


@pytest.mark.asyncio
async def test_goal_decision_submits_record_before_controller() -> None:
    store = RecordingGoalStore()
    controller = DecisionController()
    runtime = _runtime(store, phase="evaluator", controller=controller, attempt=1)

    result = await GoalDecisionTool().execute(
        {
            "status": "finished",
            "summary": "done",
            "evidence": ["tests passed"],
            "reason": "acceptance verified",
            "progress": "meaningful",
        },
        _context(runtime, session_id="evaluator-session"),
    )

    assert result.metadata["goal_decision_submitted"] is True
    assert store.records[0].phase == "decision"
    assert store.records[0].sequence_number == 2
    assert controller.calls[0]["decision"]["outcome"] == "completed"
    assert controller.calls[0]["protocol_id"] == result.metadata["protocol_id"]


@pytest.mark.asyncio
async def test_goal_decision_rejects_empty_summary_without_controller_call() -> None:
    store = RecordingGoalStore()
    controller = DecisionController()
    runtime = _runtime(store, phase="evaluator", controller=controller, attempt=1)

    result = await GoalDecisionTool().execute(
        {"status": "finished", "summary": "   "},
        _context(runtime, session_id="evaluator-session"),
    )

    assert result.metadata["error"] is True
    assert result.metadata["goal_decision_submitted"] is False
    assert controller.calls == []
    assert store.records == []


@pytest.mark.asyncio
async def test_goal_decision_is_rejected_outside_evaluator_phase() -> None:
    controller = DecisionController()
    runtime = _runtime(phase="work", controller=controller, attempt=1)

    result = await GoalDecisionTool().execute(
        {"status": "finished", "summary": "done"},
        _context(runtime, session_id="work-session"),
    )

    assert result.metadata["goal_decision_submitted"] is False
    assert controller.calls == []


@pytest.mark.asyncio
async def test_goal_init_approval_options_remain_explicit() -> None:
    store = RecordingGoalStore()
    controller = IntakeController()
    seen_prompts = []

    async def interact(request: UserInteraction) -> UserResponse:
        seen_prompts.append(request)
        return UserResponse(value="approved")

    runtime = _runtime(
        store,
        phase="intake",
        intake=controller,
        interaction=interact,
    )
    result = await GoalInitTool().execute(
        {"objective": "ship feature", "acceptance_condition": "tests pass"},
        _context(runtime, session_id="main-session"),
    )

    assert result.metadata["goal_init_submitted"] is True
    assert len(seen_prompts) == 1
    assert seen_prompts[0].timeout == 300.0
    assert {option[1] for option in seen_prompts[0].options} == {
        "approved", "revised", "cancelled",
    }


def test_legacy_goal_tool_is_not_exported() -> None:
    import voidx.agent.adapters.tools.automation.goal as goal_tools

    assert not hasattr(goal_tools, "GoalTool")
    assert "GoalTool" not in getattr(goal_tools, "__all__", ())
