import asyncio
import pytest
from langchain_core.messages import AIMessage

from voidx.agent.application.agents import AgentDef
from voidx.config import Config
from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, IntentResolution, PlanResolution
from voidx.agent.domain.task.intent import TaskIntent
from voidx.agent.adapters.tools.subagent import AgentResultContract


class FakeModel:
    def bind_tools(self, _tool_defs):
        return self


class FakeUi:
    def __init__(self):
        self.lines: list[str] = []

    def step_header(self, _persona):
        return None

    def print(self, text=""):
        self.lines.append(str(text))


class FakeEvents:
    def __init__(self):
        self.emitted: list[object] = []

    async def emit(self, event):
        self.emitted.append(event)

    def emit_direct(self, event):
        self.emitted.append(event)


class FakeUiPort:
    def __init__(self, *, events: bool = True):
        self.ui = FakeUi()
        self.events = FakeEvents()
        self.console = object()
        self._events = events

    def via_events(self):
        return self._events


def _goal_resolution() -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=GoalSpec(desc="Retry child LLM calls"),
        plan=PlanResolution(join="tdd", leave="verify"),
    )


def _result_contract() -> AgentResultContract:
    return AgentResultContract(
        schema_name="implementation_result",
        format="status, files_changed, tests_run, risks, followups",
    )


def _agent_def() -> AgentDef:
    return AgentDef(
        name="explore",
        description="test child agent",
        when_to_use="test",
        can_write=False,
        can_delegate=False,
    )


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


@pytest.mark.asyncio
async def test_run_subagent_retries_transient_llm_errors_and_cleans_retry_status(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ProviderError("rate limited", status_code=429)
        return AIMessage(content="child answer")

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    ui_port = FakeUiPort(events=True)

    output = await subagent_module.run_subagent(
        _agent_def(),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        goal_resolution=_goal_resolution(),
        result_contract=_result_contract(),
        debug=False,
        ui_port=ui_port,
    )

    retry_events = [event for event in ui_port.events.emitted if getattr(event, "status_id", None) == "llm:retry"]

    assert output == "child answer"
    assert attempts == 3
    assert sleep_delays == [0.002, 0.002]
    assert [type(event).__name__ for event in retry_events] == [
        "StatusUpdated",
        "StatusUpdated",
        "StatusFinished",
    ]


@pytest.mark.asyncio
async def test_run_subagent_retries_interrupted_upstream_response_stream(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[float] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("Upstream response stream was interrupted")
        return AIMessage(content="child answer")

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    output = await subagent_module.run_subagent(
        _agent_def(),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        goal_resolution=_goal_resolution(),
        result_contract=_result_contract(),
        debug=False,
        ui_port=FakeUiPort(events=True),
    )

    assert output == "child answer"
    assert attempts == 2
    assert sleep_delays == [0.002]


@pytest.mark.asyncio
async def test_run_subagent_recovers_context_overflow_with_partial_result(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []
    executed_tools: list[str] = []

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tool_id, _args, _ctx):
            executed_tools.append(tool_id)
            from voidx.tooling.domain.result import ToolResult

            return ToolResult(output="tool evidence")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return AIMessage(
                content="confirmed finding",
                tool_calls=[{
                    "name": "read",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }],
            )
        raise ProviderError("context_length_exceeded", status_code=400)

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    ui_port = FakeUiPort(events=True)
    run_metadata: dict[str, object] = {}

    output = await subagent_module.run_subagent(
        _agent_def(),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        goal_resolution=_goal_resolution(),
        result_contract=_result_contract(),
        run_metadata=run_metadata,
        debug=False,
        ui_port=ui_port,
    )

    assert attempts == 2
    assert executed_tools == ["read"]
    assert sleep_delays == []
    assert "confirmed finding" in output
    assert run_metadata["finish_reason"] == "context_limit"
    assert [event for event in ui_port.events.emitted if getattr(event, "status_id", None) == "llm:retry"] == []


@pytest.mark.asyncio
async def test_run_subagent_does_not_retry_non_retryable_llm_errors(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ProviderError("unauthorized", status_code=401)

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    ui_port = FakeUiPort(events=True)
    run_metadata: dict[str, object] = {}

    with pytest.raises(ProviderError):
        await subagent_module.run_subagent(
            _agent_def(),
            "Inspect child path",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            run_metadata=run_metadata,
            debug=False,
            ui_port=ui_port,
        )

    assert attempts == 1
    assert sleep_delays == []
    assert run_metadata["finish_reason"] == "error"
    assert [event for event in ui_port.events.emitted if getattr(event, "status_id", None) == "llm:retry"] == []


@pytest.mark.asyncio
async def test_run_subagent_exhausts_retryable_llm_errors(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ProviderError("server unavailable", status_code=503)

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    ui_port = FakeUiPort(events=True)
    run_metadata: dict[str, object] = {}

    with pytest.raises(ProviderError):
        await subagent_module.run_subagent(
            _agent_def(),
            "Inspect child path",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            run_metadata=run_metadata,
            debug=False,
            ui_port=ui_port,
        )

    retry_events = [event for event in ui_port.events.emitted if getattr(event, "status_id", None) == "llm:retry"]

    assert attempts == 11
    assert sleep_delays == [0.002] * 10
    assert run_metadata["finish_reason"] == "error"
    assert [type(event).__name__ for event in retry_events] == [
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusUpdated",
        "StatusFinished",
    ]


@pytest.mark.asyncio
async def test_run_subagent_retry_uses_text_fallback_without_events(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary network failure")
        return AIMessage(content="child answer")

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    ui_port = FakeUiPort(events=False)

    output = await subagent_module.run_subagent(
        _agent_def(),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        goal_resolution=_goal_resolution(),
        result_contract=_result_contract(),
        debug=False,
        ui_port=ui_port,
    )

    assert output == "child answer"
    assert attempts == 2
    assert sleep_delays == [0.002]
    assert ui_port.events.emitted == []
    assert any("Retrying" in line for line in ui_port.ui.lines)


@pytest.mark.asyncio
async def test_subagent_final_call_retries_interrupted_upstream_response_stream(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig

    attempts = 0
    sleep_delays: list[float] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("Upstream response stream was interrupted")
        return AIMessage(content="status: complete\nfindings: final summary")

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "estimate_context_tokens_with_tools", lambda *_args: 95)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    ui_port = FakeUiPort(events=True)
    output = await subagent_module.run_subagent(
        _agent_def(),
        "Inspect child path",
        "test-key",
        Config(
            workspace=str(tmp_path),
            model={"context_window": 100},
            subagent_budget=SubagentBudgetConfig(
                context_soft_ratio=0.75,
                context_hard_ratio=0.9,
            ),
        ),
        runtime_persona="explore",
        goal_resolution=_goal_resolution(),
        result_contract=_result_contract(),
        debug=False,
        ui_port=ui_port,
    )

    retry_events = [event for event in ui_port.events.emitted if getattr(event, "status_id", None) == "llm:retry"]

    assert output == "status: complete\nfindings: final summary"
    assert attempts == 2
    assert sleep_delays == [0.002]
    assert [type(event).__name__ for event in retry_events] == [
        "StatusUpdated",
        "StatusFinished",
    ]


@pytest.mark.asyncio
async def test_subagent_final_call_does_not_swallow_unknown_runtime_errors(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        raise AssertionError("connection state invariant violated")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "estimate_context_tokens_with_tools", lambda *_args: 95)

    run_metadata: dict[str, object] = {}
    with pytest.raises(AssertionError, match="connection state invariant violated"):
        await subagent_module.run_subagent(
            _agent_def(),
            "Inspect child path",
            "test-key",
            Config(
                workspace=str(tmp_path),
                model={"context_window": 100},
                subagent_budget=SubagentBudgetConfig(
                    context_soft_ratio=0.75,
                    context_hard_ratio=0.9,
                ),
            ),
            runtime_persona="explore",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            run_metadata=run_metadata,
            debug=False,
        )

    assert run_metadata["finish_reason"] == "error"


@pytest.mark.asyncio
async def test_subagent_regular_call_does_not_recover_programming_errors_from_prior_findings(
    tmp_path,
    monkeypatch,
):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.tooling.domain.result import ToolResult

    attempts = 0

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, _tool_id, _args, _ctx):
            return ToolResult(output="tool evidence")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return AIMessage(
                content="confirmed finding",
                tool_calls=[{
                    "name": "read",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }],
            )
        raise RuntimeError("programming failure")

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    run_metadata: dict[str, object] = {}
    with pytest.raises(RuntimeError, match="programming failure"):
        await subagent_module.run_subagent(
            _agent_def(),
            "Inspect child path",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            run_metadata=run_metadata,
            debug=False,
        )

    assert attempts == 2
    assert run_metadata["finish_reason"] == "error"


@pytest.mark.asyncio
async def test_run_subagent_resamples_child_runs_before_transient_retry(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-child-retry-sampling")
    parent_run_id = ""
    nested_run_id = ""
    release = asyncio.Event()
    attempts = 0
    prompts: list[str] = []

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal attempts, nested_run_id
        attempts += 1
        prompts.append("\n".join(str(message.content) for message in messages))
        if attempts == 1:
            async def nested_runner(_run_id: str) -> str:
                await release.wait()
                return "done"

            nested = await gateway.spawn(
                session_id="session-child-retry-sampling",
                parent_run_id=parent_run_id,
                agent_name="voidx",
                description="Goal: nested retry-visible child",
                runner=nested_runner,
            )
            nested_run_id = nested.run_id
            raise ProviderError("rate limited", status_code=429)
        return AIMessage(content="child answer")

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module.asyncio, "sleep", fake_sleep)

    async def parent_runner(run_id: str) -> str:
        nonlocal parent_run_id
        parent_run_id = run_id
        return await subagent_module.run_subagent(
            _agent_def(),
            "Inspect child retry sampling",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            ui_port=FakeUiPort(events=False),
        )

    parent = await gateway.spawn(
        session_id="session-child-retry-sampling",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Goal: parent retry",
        runner=parent_runner,
    )
    parent = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=parent.run_id,
        timeout=1,
    )

    assert parent.status == "completed"
    assert "Child agents: 1 running · 0 recent terminal" in prompts[1]
    assert f"{nested_run_id} [running] Goal: nested retry-visible child" in prompts[1]

    release.set()
    nested_task = gateway._runs[nested_run_id].task
    assert nested_task is not None
    await asyncio.wait_for(nested_task, timeout=1)
