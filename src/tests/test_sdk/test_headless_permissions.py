"""Permission HITL through the real graph, policy and file tool."""
import asyncio
from contextlib import aclosing

import pytest

from test_headless_runtime import FileRoundTripModel
from voidx.config import Config, PermissionMode, Settings
from voidx.sdk import VoidxAgent
from voidx.tooling.domain.interaction import InteractionResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["approve", "deny", "cancel"])
async def test_real_permission_decision(tmp_path, monkeypatch, answer):
    from voidx.llm.adapters import langchain_model_factory

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    events = []
    async with asyncio.timeout(20), VoidxAgent(config, settings=settings) as agent:
        async with aclosing(agent.stream("Write sdk-probe.txt", workspace=str(tmp_path))) as stream:
            async for event in stream:
                events.append(event)
                if event.kind == "interaction.required":
                    request = event.payload.request
                    assert request.purpose == request.input_kind == "permission"
                    assert request.tools[0].name == "write"
                    assert not (tmp_path / "sdk-probe.txt").exists()
                    if answer == "cancel":
                        await agent.cancel()
                    else:
                        value = "allow" if answer == "approve" else "deny"
                        assert value in {choice.value for choice in request.choices}
                        assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                            session_id=event.session_id, thread_id=event.thread_id,
                            turn_id=event.turn_id, value=value,
                        ))
    assert sum(event.kind == "interaction.required" for event in events) == 1
    resolved = [event for event in events if event.kind == "interaction.resolved"]
    assert len(resolved) == 1
    assert resolved[0].payload.resolution.decision == ("approved" if answer == "approve" else "deny")
    assert events[-1].kind == ("turn.cancelled" if answer == "cancel" else "turn.completed")
    assert (tmp_path / "sdk-probe.txt").exists() == (answer == "approve")
    if answer != "approve":
        assert not any(event.kind == "file.changed" for event in events)


@pytest.mark.asyncio
async def test_real_permission_event_redacts_arguments_without_mutating_call(tmp_path, monkeypatch):
    from copy import deepcopy

    from voidx.agent.adapters.langgraph.runtime.permission_flow import PermissionFlow
    from voidx.llm.adapters import langchain_model_factory
    from voidx.tooling.builtin.file.write import WriteTool

    args = {
        "file_path": "sdk-probe.txt", "op": "write", "new_string": "headless\n",
        "API-Key": "top-secret",
        "metadata": {"Authorization": "Bearer private", "items": [
            {"client_secret": "nested-secret", "label": "visible"},
            {"refresh_token": "refresh-secret"},
        ]},
    }
    original_args = deepcopy(args)

    class SensitiveArgumentsModel(FileRoundTripModel):
        def _reply(self, messages):
            reply = super()._reply(messages)
            if reply.tool_calls:
                reply.tool_calls[0]["args"] = deepcopy(args)
            return reply

    model = SensitiveArgumentsModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    decisions = []
    executed_args = []
    original_ask = PermissionFlow._ask_tool_permission
    original_write = WriteTool.execute

    async def ask(self, tool_calls, **kwargs):
        decisions.extend(tool_calls)
        result = await original_ask(self, tool_calls, **kwargs)
        for decision in tool_calls:
            assert decision.args == original_args
            assert decision.tool_call["args"] == original_args
        return result

    async def write(self, args, ctx):
        executed_args.append(deepcopy(args))
        return await original_write(self, args, ctx)

    monkeypatch.setattr(PermissionFlow, "_ask_tool_permission", ask)
    monkeypatch.setattr(WriteTool, "execute", write)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    events = []
    async with asyncio.timeout(20), VoidxAgent(config, settings=settings) as agent:
        async with aclosing(agent.stream("Write sdk-probe.txt", workspace=str(tmp_path))) as stream:
            async for event in stream:
                events.append(event)
                if event.kind == "interaction.required":
                    request = event.payload.request
                    expected = deepcopy(original_args)
                    expected["API-Key"] = "<redacted>"
                    expected["metadata"]["Authorization"] = "<redacted>"
                    expected["metadata"]["items"][0]["client_secret"] = "<redacted>"
                    expected["metadata"]["items"][1]["refresh_token"] = "<redacted>"
                    assert request.tools[0].args == expected
                    assert decisions[0].args == original_args
                    assert decisions[0].tool_call["args"] == original_args
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=event.session_id, thread_id=event.thread_id,
                        turn_id=event.turn_id, value="allow",
                    ))
    assert sum(event.kind == "interaction.required" for event in events) == 1
    assert events[-1].kind == "turn.completed"
    assert executed_args == [original_args]
    assert args == original_args
    assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"
