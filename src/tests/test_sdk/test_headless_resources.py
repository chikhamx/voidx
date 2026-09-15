"""Run-owned resource cleanup using the real execution factory."""
import asyncio
from contextlib import aclosing

import pytest

from test_headless_runtime import FileRoundTripModel
from voidx.config import Config, Settings, PermissionMode
from voidx.sdk import VoidxAgent


@pytest.fixture
def setup_agent(tmp_path, monkeypatch):
    from voidx.llm.adapters import langchain_model_factory
    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    return VoidxAgent(config, settings=settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "cancel", "restore_failure", "close_failure", "initializing_close", "initializing_cancel", "constructor_failure"])
async def test_owned_gateway_cleanup(setup_agent, tmp_path, monkeypatch, mode):
    from voidx.agent.adapters.subagent.inprocess_gateway import InProcessSubagentGateway
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    closed = []
    original = InProcessSubagentGateway.close_all
    async def close(self):
        closed.append(self)
        await original(self)
        if mode == "close_failure":
            raise RuntimeError("gateway close failed")
    monkeypatch.setattr(InProcessSubagentGateway, "close_all", close)
    entered, release = asyncio.Event(), asyncio.Event()
    original_restore = LangGraphExecution.restore_runtime_state
    async def restore(self):
        entered.set()
        if mode.startswith("initializing"):
            await release.wait()
        if mode == "restore_failure":
            raise ValueError("restore failed")
        await original_restore(self)
    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", restore)
    if mode == "constructor_failure":
        from voidx.agent.adapters.langgraph import execution
        def broken_graph(host):
            raise ValueError("constructor failed")
        monkeypatch.setattr(execution, "build_graph", broken_graph)
    agent = setup_agent
    async with asyncio.timeout(20):
        async with aclosing(agent.stream("RESTORE_PROBE", workspace=str(tmp_path))) as stream:
            if mode in {"restore_failure", "constructor_failure"}:
                with pytest.raises(ValueError, match="failed"):
                    await anext(stream)
            elif mode.startswith("initializing"):
                first = asyncio.create_task(anext(stream))
                await entered.wait()
                if mode == "initializing_cancel":
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                else:
                    closing = asyncio.create_task(agent.aclose())
                    await asyncio.sleep(0)
                    assert not closing.done()
                    release.set()
                    await closing
                    with pytest.raises(StopAsyncIteration):
                        await first
            else:
                events = [await anext(stream)]
                if mode == "cancel":
                    await agent.cancel()
                events.extend([event async for event in stream])
                expected = {"complete": "turn.completed", "cancel": "turn.cancelled", "close_failure": "turn.failed"}[mode]
                assert events[-1].kind == expected
        await agent.aclose()
        assert len(closed) == 1


@pytest.mark.asyncio
async def test_execution_close_attempts_all_owned_resources_and_is_idempotent(tmp_path):
    from test_headless_execution import make_execution
    execution, _ = make_execution(tmp_path)
    calls = []
    class Manager:
        async def stop_all(self):
            calls.append("manager")
    async def broken_gateway():
        calls.append("gateway")
        raise RuntimeError("close failed")
    execution.agent_gateway.close_all = broken_gateway
    execution._mcp_manager = Manager()
    execution._lsp_manager = Manager()
    execution._title_task = asyncio.create_task(asyncio.Event().wait())
    with pytest.raises(ExceptionGroup, match="resource cleanup"):
        await execution.aclose()
    assert calls == ["gateway", "manager", "manager"]
    assert execution._title_task.done()
    with pytest.raises(ExceptionGroup):
        await execution.aclose()
    assert calls == ["gateway", "manager", "manager"]


@pytest.mark.asyncio
async def test_execution_does_not_close_shared_dependencies(tmp_path):
    from test_headless_execution import make_execution
    class Shared:
        async def aclose(self):
            pytest.fail("Shared dependency must not be closed by an execution")
        def close(self):
            pytest.fail("Shared dependency must not be closed by an execution")
    execution, _ = make_execution(tmp_path)
    shared = Shared()
    execution.model = shared
    execution._settings = shared
    execution.model_catalog = shared
    execution.skills_api = shared
    await execution.aclose()
    assert execution.model is shared
    assert execution._settings is shared
