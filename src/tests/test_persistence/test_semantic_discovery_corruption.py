import json
import traceback

import pytest

from voidx.agent.adapters.persistence import session_repository as repository


def _record(**fields):
    return json.dumps(fields).encode() + b"\n"


async def _transcript(tmp_path):
    workspace = str(tmp_path)
    session_id = (await repository.create_session(workspace=workspace)).id
    path = repository.session_dir(session_id) / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    start = _record(type="turn_start", turn_id="turn-1")
    node = _record(type="node", turn_id="turn-1", metadata={
        "semantic_identity": {"session_id": session_id, "thread_id": "thread-1"}})
    end = _record(type="turn_end", turn_id="turn-1")
    return workspace, session_id, path, start, node, end


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    b'{"private-secret": broken}\n',
    b'{"private-secret":\n',
    b'{"private-secret": "\xff"}\n',
    b'[]\n', b'null\n', b'"private-secret"\n',
    _record(type="node", turn_id="turn-1", metadata=[]),
    _record(type="node", turn_id="turn-1", metadata=None),
    _record(type="node", turn_id="turn-1", metadata="private-secret"),
])
async def test_discovery_rejects_corrupt_middle_record(tmp_path, bad):
    workspace, session_id, path, start, node, end = await _transcript(tmp_path)
    path.write_bytes(start + bad + node + end)
    with pytest.raises(ValueError) as error:
        await repository.semantic_thread_bindings(workspace)
    message = str(error.value)
    assert session_id in message
    assert "transcript.jsonl" in message
    assert "line 2" in message
    assert "private-secret" not in "".join(traceback.format_exception(error.value))
    if b'metadata' in bad:
        assert "metadata" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [
    b'{"private-secret": broken}\n', b'{"private-secret": broken}',
    b'{"private-secret":\n', b'{"private-secret": "\xff"}',
    b'[]', b'null', b'{}\xe4\xb8', b'{"type":\xe4\xb8',
])
async def test_discovery_rejects_corrupt_final_record(tmp_path, tail):
    workspace, session_id, path, start, node, end = await _transcript(tmp_path)
    path.write_bytes(start + node + end + tail)
    with pytest.raises(ValueError) as error:
        await repository.semantic_thread_bindings(workspace)
    assert session_id in str(error.value)
    assert "transcript.jsonl" in str(error.value)
    assert "line 4" in str(error.value)
    assert "private-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [b'', b'{"type":', b'{"type":"unfinished',
    b'{"type":"\xe4\xb8', b'{"type":tru'])
async def test_discovery_tolerates_incomplete_unterminated_tail(tmp_path, tail):
    workspace, session_id, path, start, node, end = await _transcript(tmp_path)
    path.write_bytes(start + node + end + tail)
    assert await repository.semantic_thread_bindings(workspace) == {"thread-1": session_id}


@pytest.mark.asyncio
async def test_discovery_reads_complete_record_without_newline(tmp_path):
    workspace, session_id, path, start, node, end = await _transcript(tmp_path)
    path.write_bytes(start + node + end.rstrip(b'\n'))
    assert await repository.semantic_thread_bindings(workspace) == {"thread-1": session_id}
