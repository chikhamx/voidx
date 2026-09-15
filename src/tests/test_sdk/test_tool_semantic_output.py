"""Real file tools publish bounded semantic output without a UI host."""
import asyncio
from types import SimpleNamespace

import pytest

from voidx.agent.application.runtime.semantic_channel import SemanticChannel
from voidx.agent.domain.display_policy import ToolDisplayPolicy
from voidx.agent.adapters.langgraph.runtime.tool_executor.ui import (
    notify_tool_started, notify_tool_result, notify_tool_diff,
)
from voidx.tooling.application.execution import FileToolContext
from voidx.tooling.builtin.file.write import WriteTool


@pytest.mark.asyncio
async def test_real_write_publishes_semantic_output_without_ui(tmp_path):
    from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput

    channel = SemanticChannel(session_id="session", thread_id="thread", turn_id="turn", capacity=1)
    host = SimpleNamespace(semantic_output=SemanticOutput(channel, **channel.identity))
    call = {"id": "write-1", "name": "write", "args": {
        "file_path": "probe.txt", "op": "write", "new_string": "hello\n",
    }}
    policy = ToolDisplayPolicy.from_config({}, defaults={})
    await notify_tool_started(host, call, policy)
    result = await WriteTool().execute(call["args"], FileToolContext(workspace=str(tmp_path)))
    pending = asyncio.create_task(notify_tool_result(host, call, result, True, 0.1, policy, None))
    await asyncio.sleep(0)
    assert not pending.done(), "Semantic producers must await queue backpressure"
    stream = channel.events()
    started = await anext(stream)
    finished = await anext(stream)
    output = await anext(stream)
    await pending
    await notify_tool_diff(host, result, "write-1", None)
    changed = await anext(stream)
    await stream.aclose()
    assert [e.kind for e in (started, finished, output, changed)] == [
        "tool.started", "tool.finished", "tool.result", "file.changed",
    ]
    assert [e.sequence for e in (started, finished, output, changed)] == [1, 2, 3, 4]
    assert started.payload.arguments == call["args"]
    assert output.payload.summary == (result.summary or result.output)
    assert changed.payload.path == "probe.txt"
    assert changed.payload.diff == result.diff
    assert (tmp_path / "probe.txt").read_text() == "hello\n"


def test_graph_execution_accepts_semantic_output_dependency():
    import inspect
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    parameter = inspect.signature(LangGraphExecution).parameters["semantic_output"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_supervisor_seals_failure_when_tool_publication_exceeds_limit():
    from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput
    from voidx.agent.application.runtime.run_supervisor import RunSupervisor

    channel = SemanticChannel(session_id="s", thread_id="t", turn_id="r", max_event_bytes=1024)
    output = SemanticOutput(channel, **channel.identity)
    persisted = []

    async def execute(supervisor):
        await output.tool_started({"id": "call", "name": "write", "args": {"new_string": "x" * 2048}})
        pytest.fail("Oversize events must not silently disappear")

    async def persist(result):
        persisted.append(result)

    async def cleanup(outcome):
        assert outcome == "failed"
        return ()

    supervisor = RunSupervisor(channel, execute=execute, persist=persist, cleanup=cleanup)
    events = [event async for event in supervisor.events()]
    assert [event.kind for event in events] == ["turn.failed"]
    assert not persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_key", [
    "api_key", "API-Key", "apiKey", "Api.Key", " API KEY ",
    "token", "ToKeN", "access_token", "refresh-token", "idToken",
    "password", "Pass-Word", "secret", "client_secret",
    "authorization", "AUTHORIZATION", "Proxy-Authorization",
    "cookie", "Set-Cookie",
])
async def test_tool_started_redacts_nested_sensitive_keys_without_mutating_call(sensitive_key):
    from copy import deepcopy

    from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput

    channel = SemanticChannel(session_id="s", thread_id="t", turn_id="r")
    output = SemanticOutput(channel, **channel.identity)
    ordinary = {
        "file_path": "notes.txt", "new_string": "Ordinary file body\n",
        "content": "Keep this content", "body": "Keep this body",
        "max_tokens": 512, "nested": ["plain text", {"enabled": True}],
    }
    call = {"id": "call", "name": "write", "args": {
        **ordinary,
        sensitive_key: "top-level-credential",
        "options": {sensitive_key: {"value": "nested-credential"}},
        "items": [{"deeper": [{sensitive_key: ["list-credential"]}]}],
    }}
    original = deepcopy(call)
    expected = {
        **deepcopy(ordinary),
        sensitive_key: "<redacted>",
        "options": {sensitive_key: "<redacted>"},
        "items": [{"deeper": [{sensitive_key: "<redacted>"}]}],
    }

    await output.tool_started(call)
    stream = channel.events()
    event = await anext(stream)
    await stream.aclose()

    assert event.kind == "tool.started"
    assert event.payload.arguments == expected
    assert call == original
    serialized = event.model_dump_json()
    for credential in ("top-level-credential", "nested-credential", "list-credential"):
        assert credential not in serialized
    call["args"]["nested"][1]["enabled"] = False
    assert event.payload.arguments == expected
    event.payload.arguments["items"][0]["deeper"].append("event-only")
    assert call["args"]["items"] == original["args"]["items"]
