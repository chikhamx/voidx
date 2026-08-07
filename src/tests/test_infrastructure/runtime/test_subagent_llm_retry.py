import pytest
from langchain_core.messages import AIMessage

from voidx.agent.application.agents import AgentDef
from voidx.config import Config
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
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
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
async def test_run_subagent_does_not_retry_context_overflow(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        nonlocal attempts
        attempts += 1
        raise ProviderError("context_length_exceeded", status_code=400)

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
async def test_run_subagent_does_not_retry_non_retryable_llm_errors(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
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
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
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
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    attempts = 0
    sleep_delays: list[int] = []

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("temporary network failure")
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
