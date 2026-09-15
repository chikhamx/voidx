"""Real intake controllers must not commit before semantic approval."""
import asyncio
from dataclasses import fields

import pytest

from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime
from voidx.agent.adapters.tools.automation.goal import GoalInitTool
from voidx.agent.adapters.tools.automation.loop import LoopInitTool
from voidx.agent.application.automation.goal.intake_controller import GoalIntakeController
from voidx.agent.application.automation.loop.intake_controller import LoopIntakeController
from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
from voidx.agent.application.runtime.run_ownership import SharedInteractionBudget
from voidx.tooling.domain.interaction import InteractionResponse

IDENTITY = dict(session_id="actual-session", thread_id="actual-thread", turn_id="actual-turn")


class Store:
    def __init__(self):
        self.records = []

    async def submit_goal_protocol(self, record, **kwargs):
        self.records.append(record)
        return record


class Publisher:
    def __init__(self):
        self.events = []
        self._interaction_cleanups = []
        self.required = asyncio.Event()

    async def publish(self, event):
        self.events.append(event)
        if event.kind == "interaction.required":
            self.required.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("answer", ["approved", "revised", "cancelled", "free_text", "dismissed", "timed_out", "task_cancelled"])
async def test_real_init_waits_for_coordinator(kind, answer, monkeypatch):
    publisher = Publisher()
    budget = SharedInteractionBudget(4)
    coordinator = InteractionCoordinator(publisher, **IDENTITY, budget=budget)
    controller = GoalIntakeController() if kind == "goal" else LoopIntakeController()
    store = Store()

    async def forbidden_legacy(_):
        pytest.fail("Semantic initialization fell back to UserResponse")

    runtime = AgentToolRuntime(
        goal_intake=controller, loop_intake=controller, goal_phase="idle", loop_phase="idle",
        goal_store=store, goal_generation="generation", goal_parent_session_id="parent",
        goal_main_session_id=IDENTITY["session_id"], goal_turn_id=IDENTITY["turn_id"],
        interaction=forbidden_legacy, interaction_identity=IDENTITY,
    )
    # Assign after construction so pre-implementation RED exercises business behavior.
    from types import SimpleNamespace
    from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _bind_autonomous_requester

    runtime.autonomous_requester = _bind_autonomous_requester(SimpleNamespace(
        interaction_requester=coordinator.request, semantic_output=SimpleNamespace(identity=IDENTITY),
    ))
    module = f"voidx.agent.adapters.tools.automation.{kind}"
    timeout_name = "_INIT_APPROVAL_TIMEOUT_SECONDS" if kind == "goal" else "_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS"
    monkeypatch.setattr(f"{module}.{timeout_name}", 0.01 if answer == "timed_out" else 10.0)
    ctx = AgentToolExecutionContext(workspace="/tmp", session_id="not-the-turn", runtime=runtime)
    tool = GoalInitTool() if kind == "goal" else LoopInitTool()
    task = asyncio.create_task(tool.execute(dict(goal="Ship feature", acceptance_condition="Tests pass"), ctx))
    waiting = asyncio.create_task(publisher.required.wait())
    try:
        done, _ = await asyncio.wait({task, waiting}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert waiting in done, "init never requested semantic approval"
        assert controller.final_spec() is None
        assert store.records == []
        request_value = publisher.events[0].payload.request
        assert request_value.purpose == kind
        assert {key: getattr(request_value, key) for key in IDENTITY} == IDENTITY
        assert getattr(request_value, kind) is not None
        assert [choice.value for choice in request_value.choices] == ["approved", "revised", "cancelled"]
        if answer == "task_cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            tail = await coordinator.cleanup("cancelled")
            assert tail[0].payload.resolution.resolution_reason == "task_cancelled"
            for cleanup in publisher._interaction_cleanups:
                cleanup()
        else:
            if answer != "timed_out":
                assert await coordinator.submit_interaction(request_value.interaction_id, InteractionResponse(
                    **IDENTITY, value="approved" if answer == "free_text" else answer,
                    free_text=answer == "free_text", cancelled=answer == "dismissed",
                ))
            result = await asyncio.wait_for(task, 2)
            assert result.metadata[f"{kind}_init_submitted"] is (answer == "approved")
        assert (controller.final_spec() is not None) is (answer == "approved")
        assert len(store.records) == (1 if kind == "goal" and answer == "approved" else 0)
        assert budget.count == 0
    finally:
        for pending in (task, waiting):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, waiting, return_exceptions=True)
        await coordinator.cleanup("completed")


def test_runtime_declares_explicit_autonomous_capability():
    assert "autonomous_requester" in {field.name for field in fields(AgentToolRuntime)}


@pytest.mark.asyncio
async def test_autonomous_binding_issues_owner_id_and_uses_turn_identity():
    from types import SimpleNamespace
    from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _bind_autonomous_requester
    from voidx.tooling.domain.interaction import InteractionRequest

    publisher = Publisher()
    budget = SharedInteractionBudget(1)
    coordinator = InteractionCoordinator(publisher, **IDENTITY, budget=budget)
    host = SimpleNamespace(interaction_requester=coordinator.request,
                           semantic_output=SimpleNamespace(identity=IDENTITY))
    requester = _bind_autonomous_requester(host)
    task = asyncio.create_task(requester(InteractionRequest(
        **dict(session_id="stale", thread_id="stale", turn_id="stale"),
        interaction_id="goal-init", purpose="goal", input_kind="choice", prompt="Approve?",
        choices=[dict(label="Approve", value="approved")],
    )))
    try:
        await asyncio.wait_for(publisher.required.wait(), 2)
        request = publisher.events[0].payload.request
        assert request.interaction_id != "goal-init"
        assert {key: getattr(request, key) for key in IDENTITY} == IDENTITY
        assert await coordinator.submit_interaction(request.interaction_id, InteractionResponse(**IDENTITY, value="approved"))
        assert (await task).decision == "approved"
        assert budget.count == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await coordinator.cleanup("completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
async def test_executor_binds_autonomous_capability(tmp_path, kind, monkeypatch):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage
    from tests.langgraph_execution import make_langgraph_execution
    from voidx.config import Config
    from voidx.tooling.domain.result import ToolResult

    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key="test-key")
    coordinator = InteractionCoordinator(Publisher(), **IDENTITY, budget=SharedInteractionBudget(1))
    graph.interaction_requester = coordinator.request
    graph.semantic_output = SimpleNamespace(identity=IDENTITY)
    captured = []

    def capture(registry, runtime):
        captured.append(runtime)

    async def allow_all(calls, **kwargs):
        return [], []

    monkeypatch.setattr("voidx.agent.adapters.langgraph.runtime.tool_executor.executor.bind_agent_tool_runtime", capture)
    graph._authorize_tool_calls = allow_all
    from voidx.agent.adapters.langgraph.runtime.thread_context import bind_thread_execution_context
    from voidx.agent.domain.turn_context import TurnExecutionContext

    async with bind_thread_execution_context(graph, turn_context=TurnExecutionContext(
        session_id="", thread_id=IDENTITY["thread_id"], workspace=str(tmp_path),
    )):
        await graph._execute_tools(dict(
            messages=[AIMessage(content="", tool_calls=[dict(name=f"{kind}_init", args={}, id="init-call", type="tool_call")])],
            workspace=str(tmp_path), persona="voidx", plan_mode=False,
        ))
    assert len(captured) == 1
    assert captured[0].autonomous_requester is not None
    assert captured[0].interaction_identity == IDENTITY


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
async def test_semantic_init_without_requester_does_not_submit(kind):
    from types import SimpleNamespace
    from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _bind_autonomous_requester

    controller = GoalIntakeController() if kind == "goal" else LoopIntakeController()
    store = Store()
    runtime = AgentToolRuntime(
        goal_intake=controller, loop_intake=controller, goal_phase="idle", loop_phase="idle",
        goal_store=store, goal_generation="generation", goal_parent_session_id="parent",
        goal_main_session_id=IDENTITY["session_id"], goal_turn_id=IDENTITY["turn_id"],
        interaction_identity=IDENTITY,
        autonomous_requester=_bind_autonomous_requester(SimpleNamespace(
            interaction_requester=None, semantic_output=SimpleNamespace(identity=IDENTITY),
        )),
    )
    tool = GoalInitTool() if kind == "goal" else LoopInitTool()
    result = await tool.execute(dict(goal="Ship feature", acceptance_condition="Tests pass"),
                                AgentToolExecutionContext(workspace="/tmp", runtime=runtime))
    assert result.metadata[f"{kind}_init_submitted"] is False
    assert controller.final_spec() is None
    assert store.records == []


def test_legacy_without_semantic_capabilities_keeps_legacy_binding():
    from types import SimpleNamespace
    from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _bind_autonomous_requester
    assert _bind_autonomous_requester(SimpleNamespace()) is None
