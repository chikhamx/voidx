"""Real-graph SDK acceptance test; only the provider model is deterministic."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import aclosing
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr


class FileRoundTripModel(BaseChatModel):
    _histories: list[list] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "headless-file-round-trip"

    def bind_tools(self, tools, **kwargs):
        return self

    def _reply(self, messages):
        self._histories.append(list(messages))
        latest_user = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
        if "RESTORE_PROBE" in str(latest_user.content):
            return AIMessage(content="RESTORED")
        if any(isinstance(m, ToolMessage) and m.tool_call_id == "write-probe" for m in messages):
            return AIMessage(content="FILE_WRITTEN")
        return AIMessage(content="", tool_calls=[{
            "id": "write-probe",
            "name": "write",
            "args": {"file_path": "sdk-probe.txt", "op": "write", "new_string": "headless\n"},
            "type": "tool_call",
        }])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self._reply(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=reply.content, tool_calls=reply.tool_calls,
        ))


@pytest.mark.asyncio
async def test_real_graph_file_tool_persistence_and_headless_restore(tmp_path: Path, monkeypatch):
    from voidx.agent.adapters.langgraph import execution
    from voidx.agent.adapters.langgraph.runtime.session_runtime import SessionRuntime
    from voidx.agent.adapters.langgraph.runtime.turn_runner import TurnRunner
    from voidx.agent.adapters.persistence.session_repository import get_session, load_messages
    from voidx.config import Config, PermissionMode, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.tooling.builtin.file.write import WriteTool

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test-not-a-credential")

    calls = {"graph": 0, "turn": 0, "write": 0, "restore": 0}
    original_build = execution.build_graph
    original_turn = TurnRunner.run_once
    original_write = WriteTool.execute
    original_restore = SessionRuntime.restore_runtime_state

    def build_graph(host):
        calls["graph"] += 1
        graph = original_build(host)
        assert {"prepare", "call_llm", "execute_tools", "finalize"} <= set(graph.nodes)
        return graph

    async def run_once(self, *args, **kwargs):
        calls["turn"] += 1
        return await original_turn(self, *args, **kwargs)

    async def write(self, args, ctx):
        calls["write"] += 1
        return await original_write(self, args, ctx)

    async def restore(self):
        calls["restore"] += 1
        return await original_restore(self)

    async def forbidden_snapshot(*args, **kwargs):
        pytest.fail("Headless restoration must not enter the presentation snapshot/tree path")

    monkeypatch.setattr(execution, "build_graph", build_graph)
    monkeypatch.setattr(TurnRunner, "run_once", run_once)
    monkeypatch.setattr(WriteTool, "execute", write)
    monkeypatch.setattr(SessionRuntime, "restore_runtime_state", restore)
    monkeypatch.setattr(SessionRuntime, "restore_transcript_snapshot", forbidden_snapshot)

    # Guard cached imports too: a meta-path finder alone misses those.
    original_import = builtins.__import__

    def headless_import(name, *args, **kwargs):
        if name == "voidx.presentation" or name.startswith("voidx.presentation."):
            pytest.fail(f"Headless execution imported presentation: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", headless_import)
    from voidx.sdk import VoidxAgent

    async def consume(agent, prompt, session_id=""):
        async with aclosing(agent.stream(prompt, session_id=session_id, workspace=str(tmp_path))) as stream:
            return [event async for event in stream]

    async with asyncio.timeout(20):
        async with VoidxAgent(config, settings=settings) as agent:
            events = await consume(agent, "Write headless followed by a newline to sdk-probe.txt.")

        assert calls["graph"] >= 1
        assert calls["turn"] == 1
        assert calls["write"] == 1
        assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"
        kinds = [event.kind for event in events]
        assert kinds[0] == "turn.started"
        assert kinds[-1] == "turn.completed"
        assert sum(kind in {"turn.completed", "turn.failed", "turn.cancelled"} for kind in kinds) == 1
        assert kinds.index("tool.started") < kinds.index("tool.finished")
        assert "tool.result" in kinds
        assert "file.changed" in kinds
        assert kinds.index("assistant.stream_started") < kinds.index("assistant.committed")
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert len({event.event_id for event in events}) == len(events)
        session_id = events[0].session_id
        assert session_id and all(event.session_id == session_id for event in events)
        assert all(event.thread_id and event.turn_id for event in events)
        assert await get_session(session_id) is not None
        rows = await load_messages(session_id)
        assert {"user", "assistant", "tool"} <= {row.role for row in rows}

        restores_before = calls["restore"]
        async with VoidxAgent(config, settings=settings) as restored:
            replay = await consume(restored, "RESTORE_PROBE: confirm the prior tool result.", session_id)
        assert calls["restore"] > restores_before
        assert calls["graph"] >= 2
        assert calls["turn"] == 2
        assert calls["write"] == 1, "Restoring must not replay file side effects"
        assert replay[-1].kind == "turn.completed"
        assert replay[0].session_id == session_id
        assert replay[0].turn_id != events[0].turn_id
        restored_histories = [history for history in model._histories if any(
            isinstance(message, HumanMessage) and "RESTORE_PROBE" in str(message.content)
            for message in history
        )]
        assert restored_histories
        assert any(isinstance(message, ToolMessage) and message.tool_call_id == "write-probe"
                   for message in restored_histories[-1])
        assert not list(tmp_path.rglob("transcript.jsonl"))
