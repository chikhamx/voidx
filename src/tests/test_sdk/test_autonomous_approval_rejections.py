"""Durable rejection acceptance through SDK, LangGraph and real intake controllers."""
import asyncio
import sqlite3

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.application.automation.loop.loop_service import LoopService
from voidx.agent.application.runtime.pump import WakeupPumpMixin
from voidx.agent.application.runtime.runtime import AgentRuntime
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.persistence import sqlite as database
from voidx.sdk import VoidxAgent
from voidx.tooling.domain.interaction import InteractionResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["goal", "loop"])
@pytest.mark.parametrize("answer", ["revised", "cancelled", "timed_out", "missing_requester", "free_text"])
async def test_sdk_rejected_init_has_no_durable_automation(tmp_path, monkeypatch, profile, answer):
    feedback = "Please require a separate regression suite before proceeding."
    histories, requests, executions, tool_results, service_calls = [], [], [], [], []

    class InitThenFinishModel(FileRoundTripModel):
        def _reply(self, messages):
            histories.append(list(messages))
            assert len(histories) <= 2, "Rejected intake unexpectedly started another model turn"
            if len(histories) == 1:
                args = {"goal": "Ship the acceptance matrix"}
                if profile == "goal":
                    args["acceptance_condition"] = "All regression tests pass"
                return AIMessage(content="", tool_calls=[{
                    "id": "rejected-init", "name": f"{profile}_init", "args": args,
                }])
            return AIMessage(content="Intake ended without approval.")

    model = InitThenFinishModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    original_run_turn = AgentRuntime.run_turn

    async def record_turn(self, request):
        requests.append(request)
        execution = self._resources.turn_engine._execution
        assert isinstance(execution, LangGraphExecution)
        executions.append(execution)
        return await original_run_turn(self, request)

    monkeypatch.setattr(AgentRuntime, "run_turn", record_turn)
    if answer == "missing_requester":
        from voidx.agent.adapters.langgraph.runtime.tool_executor import executor
        original_bind = executor._bind_autonomous_requester

        def without_capability(host):
            # Withdraw only the capability at the actual tool-runtime binding boundary.
            requester = host.interaction_requester
            assert requester is not None
            host.interaction_requester = None
            try:
                return original_bind(host)
            finally:
                host.interaction_requester = requester

        monkeypatch.setattr(executor, "_bind_autonomous_requester", without_capability)
    from voidx.agent.adapters.tools.automation.goal import GoalInitTool
    from voidx.agent.adapters.tools.automation.loop import LoopInitTool
    tool_class = GoalInitTool if profile == "goal" else LoopInitTool
    original_execute = tool_class.execute

    async def record_result(self, args, ctx):
        result = await original_execute(self, args, ctx)
        tool_results.append(result)
        controller = ctx.runtime.goal_intake if profile == "goal" else ctx.runtime.loop_intake
        assert controller.final_spec() is None
        assert isinstance(ctx.runtime.goal_store, ThreadStore) if profile == "goal" else True
        return result

    monkeypatch.setattr(tool_class, "execute", record_result)
    for service_class in (GoalService, LoopService):
        original_start = service_class.start

        async def record_start(self, *args, _original=original_start, **kwargs):
            service_calls.append(type(self).__name__ + ".start")
            return await _original(self, *args, **kwargs)

        monkeypatch.setattr(service_class, "start", record_start)
    original_pump = WakeupPumpMixin.start_pump

    def record_pump(self):
        service_calls.append(type(self).__name__ + ".start_pump")
        return original_pump(self)

    monkeypatch.setattr(WakeupPumpMixin, "start_pump", record_pump)
    if answer == "timed_out":
        constant = "_INIT_APPROVAL_TIMEOUT_SECONDS" if profile == "goal" else "_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS"
        monkeypatch.setattr(f"voidx.agent.adapters.tools.automation.{profile}.{constant}", 0.05)

    session = await create_session(workspace=str(tmp_path), profile=profile)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test-not-a-credential")
    events, approvals = [], []
    run = None
    async with VoidxAgent(config, settings=settings) as agent:
        async with asyncio.timeout(20):
            async for event in agent.stream("Ship the acceptance matrix", session_id=session.id,
                                            workspace=str(tmp_path)):
                events.append(event)
                run = agent._run
                if event.kind == "interaction.required":
                    request = event.payload.request
                    approvals.append(request)
                    assert request.purpose == profile
                    assert request.session_id == request.thread_id == session.id
                    assert request.turn_id == events[0].turn_id
                    if answer != "timed_out":
                        assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                            session_id=request.session_id, thread_id=request.thread_id,
                            turn_id=request.turn_id, value=feedback if answer == "free_text" else answer,
                            free_text=answer == "free_text",
                        ))

    assert len(approvals) == (0 if answer == "missing_requester" else 1)
    assert len(requests) == len(executions) == len(tool_results) == 1
    assert executions[0].graph is not None
    assert len(histories) == (2 if profile == "loop" or answer in ("revised", "free_text") else 1)
    assert tool_results[0].metadata[f"{profile}_init_submitted"] is False
    assert service_calls == []
    started = [event for event in events if event.kind == "turn.started"]
    terminals = [event for event in events if event.kind in ("turn.completed", "turn.failed", "turn.cancelled")]
    assert len(started) == len(terminals) == 1
    assert terminals[0].kind == "turn.completed"
    assert terminals[0].turn_id == started[0].turn_id
    assert {event.session_id for event in started + terminals} == {session.id}
    assert run._driver.done()
    assert run.owner.producer_count == run.owner.mux.registered == 0
    assert (await run.owner.completion).outcome == "completed"
    assert run._interactions == {}
    assert run._session_lock._handle is None
    assert run._workspace_lock._closed
    assert run._workspace_lock._lock._handle is None

    # An independent connection proves persisted absence, not an in-memory store view.
    db_path = database.DATA_DIR / "store" / "voidx.db"
    reopened = ThreadStore(db_path)
    try:
        assert await reopened.list_pending_outbox(session.id) == []
        with sqlite3.connect(db_path) as connection:
            for table in ("goal_protocol_records", "goal_generations", "runtime_outbox",
                          "goal_public_summary_outbox"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
            rows = connection.execute("SELECT id, parent_thread_id FROM agent_threads").fetchall()
            assert all(thread_id == session.id and parent is None for thread_id, parent in rows), rows
            assert connection.execute("SELECT id FROM sessions").fetchall() == [(session.id,)]
        loaded = await reopened.load(session.id)
        if loaded is not None:
            assert not loaded.state.context.get("goal_spec")
            assert not loaded.state.context.get("goal_run")
            assert not loaded.state.context.get("loop_spec")
    finally:
        reopened._conn.close()

    if answer in ("revised", "free_text"):
        returned = [message for message in histories[1] if isinstance(message, ToolMessage)
                    and message.tool_call_id == "rejected-init"]
        assert len(returned) == 1
        assert tool_results[0].metadata[f"{profile}_init_decision"] == "revised"
        if answer == "free_text":
            assert feedback in str(returned[0].content), "Revision feedback was lost from the real tool return"
