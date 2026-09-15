"""Public SDK lifecycle contracts, using the real factory and graph."""
from contextlib import aclosing

import pytest

from test_headless_runtime import FileRoundTripModel


@pytest.mark.asyncio
async def test_lazy_single_stream_cancel_and_close(tmp_path, monkeypatch):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, Settings, PermissionMode
    from voidx.llm.adapters import langchain_model_factory

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    agent = VoidxAgent(config, settings=settings)
    unused = agent.stream("unused")
    assert not model._histories
    async with aclosing(agent.stream("RESTORE_PROBE", workspace=str(tmp_path))) as first:
        started = await anext(first)
        assert started.kind == "turn.started"
        async with aclosing(agent.stream("second", workspace="/not-used")) as second:
            with pytest.raises(RuntimeError):
                await anext(second)
        with pytest.raises(ValueError):
            await agent.cancel(session_id="wrong")
        await agent.cancel(session_id=started.session_id)
        rest = [event async for event in first]
        assert rest[-1].kind == "turn.cancelled"
    await agent.aclose()
    await agent.aclose()
    await agent.cancel()
    with pytest.raises(RuntimeError):
        await anext(unused)
    with pytest.raises(RuntimeError):
        await agent.submit_interaction("missing", None)


@pytest.mark.asyncio
async def test_early_iterator_close_stops_run(tmp_path, monkeypatch):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, Settings
    from voidx.llm.adapters import langchain_model_factory

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    async with VoidxAgent(config, settings=settings) as agent:
        async with aclosing(agent.stream("RESTORE_PROBE", workspace=str(tmp_path))) as events:
            assert (await anext(events)).kind == "turn.started"
        assert agent._run is None
        assert await agent.submit_interaction("missing", None) is False


@pytest.mark.asyncio
async def test_agent_close_waits_for_active_run(tmp_path, monkeypatch):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, Settings
    from voidx.llm.adapters import langchain_model_factory

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    agent = VoidxAgent(config, settings=settings)
    async with aclosing(agent.stream("RESTORE_PROBE", workspace=str(tmp_path))) as events:
        assert (await anext(events)).kind == "turn.started"
        run = agent._run
        await agent.aclose()
        assert run._task.done()
        with pytest.raises(RuntimeError):
            await anext(agent.stream("closed"))
        await agent.aclose()


@pytest.mark.asyncio
async def test_aclose_reports_cleanup_failure(tmp_path, monkeypatch):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.agent.adapters.subagent.inprocess_gateway import InProcessSubagentGateway

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    async def broken_close(self):
        raise RuntimeError("gateway failure")
    monkeypatch.setattr(InProcessSubagentGateway, "close_all", broken_close)
    config = Config(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    agent = VoidxAgent(config, settings=settings)
    async with aclosing(agent.stream("RESTORE_PROBE", workspace=str(tmp_path))) as stream:
        await anext(stream)
        with pytest.raises(RuntimeError, match="failed"):
            await agent.aclose()
