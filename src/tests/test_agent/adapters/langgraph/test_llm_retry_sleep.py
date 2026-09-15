"""Tests for LLM retry sleep behavior."""

import pytest

from voidx.agent.adapters.langgraph.runtime.core import loop as loop_module
from voidx.agent.adapters.langgraph.runtime.core.helpers import LLMErrorKind
from langchain_core.messages import HumanMessage


class _SilentUi:
    class _Printer:
        def print(self, *_args, **_kwargs) -> None:
            pass

    ui = _Printer()

    def via_events(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_llm_retry_sleep_uses_agent_test_simulated_delay(monkeypatch):
    observed_delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        observed_delays.append(delay)

    monkeypatch.setattr(loop_module.asyncio, "sleep", record_sleep)

    result = await loop_module.handle_llm_exception(
        ui=_SilentUi(),
        loop=loop_module.LlmLoopState(context_tokens=0),
        error=ConnectionError("boom"),
        kind=LLMErrorKind.NETWORK,
        max_retries=1,
        timeout_max_retries=1,
    )

    assert result.action == "retry"
    assert observed_delays == [0.002]
    assert result.action == "retry"
    assert observed_delays == [0.002]


@pytest.mark.asyncio
async def test_handle_llm_exception_tolerates_none_ui(monkeypatch):
    observed_delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        observed_delays.append(delay)

    monkeypatch.setattr(loop_module.asyncio, "sleep", record_sleep)

    loop = loop_module.LlmLoopState(context_tokens=0)
    result = await loop_module.handle_llm_exception(
        ui=None,
        loop=loop,
        error=ConnectionError("boom"),
        kind=LLMErrorKind.NETWORK,
        max_retries=1,
        timeout_max_retries=1,
    )
    assert result.action == "retry"

    result = await loop_module.handle_llm_exception(
        ui=None,
        loop=loop,
        error=ConnectionError("boom"),
        kind=LLMErrorKind.NON_RETRYABLE,
        max_retries=1,
        timeout_max_retries=1,
    )
    assert result.action == "fail"
    assert result.failure_text

    loop.retry_status_active = True
    result = await loop_module.handle_llm_exception(
        ui=None,
        loop=loop,
        error=TimeoutError("slow"),
        kind=LLMErrorKind.TIMEOUT,
        max_retries=0,
        timeout_max_retries=0,
    )
    assert result.action == "fail"
    assert result.failure_text
    loop.retry_status_active = True
    result = await loop_module.handle_llm_exception(
        ui=None,
        loop=loop,
        error=TimeoutError("slow"),
        kind=LLMErrorKind.TIMEOUT,
        max_retries=0,
        timeout_max_retries=0,
    )
    assert result.action == "fail"
    assert result.failure_text


@pytest.mark.asyncio
async def test_headless_call_llm_survives_llm_exception(tmp_path, monkeypatch):
    import asyncio
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput
    from voidx.agent.ports.workspace_lock import NullWorkspaceWriteLock
    from voidx.config import Config
    from voidx.llm.domain.model import ReasoningEffort
    from voidx.llm.domain.provider import get_context_limit
    from voidx.skills.application.api import SkillsApi
    from voidx.skills.registry import SkillRegistry
    from voidx.skills.service import SkillService
    from voidx.tooling.adapters.permission.in_memory_state import create_permission_service
    from voidx.bootstrap.tooling import build_tool_registry, bind_scoped_tools, scoped_tool_registry

    class Publisher:
        def __init__(self):
            self.events = []

        async def publish(self, event):
            self.events.append(event)

    class ExplodingModel:
        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            raise ConnectionError("provider down")

    publisher = Publisher()
    skills = SkillsApi(SkillService(SkillRegistry(str(tmp_path))))

    def permission(config, *, settings, notifier):
        return create_permission_service(permission_mode=config.permission_mode.value, notifier=notifier)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    execution = LangGraphExecution(
        Config(workspace=str(tmp_path)),
        api_key="test-key",
        ui=None,
        workspace_write_lock=NullWorkspaceWriteLock(),
        semantic_output=SemanticOutput(publisher, session_id="s", thread_id="t", turn_id="r"),
        permission_notifier=lambda text: publisher.events.append(text),
        skills_api=skills,
        skills_api_provider=lambda workspace: skills,
        permission_service_factory=permission,
        model_factory=lambda *args: None,
        resolver_model_factory=lambda *args: None,
        tool_registry_factory=build_tool_registry,
        scoped_tools_binder=bind_scoped_tools,
        profile_tool_registry_factory=scoped_tool_registry,
        slash_handler_factory=lambda host: None,
        reasoning_effort_type=ReasoningEffort,
        context_limit_resolver=get_context_limit,
        provider_specs={},
        language_labels={},
        tone_labels={},
    )
    execution.model = ExplodingModel()

    result = await execution._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
    })

    assert result["should_continue"] is False
    assert result["messages"] == []