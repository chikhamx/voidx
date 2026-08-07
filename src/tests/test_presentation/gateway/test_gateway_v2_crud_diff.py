"""Tests for v2 JSON-RPC gateway session and server.

The v2 gateway replaces v1 envelope broadcasting with:
- WorkspaceSnapshot on connect (v2 model)
- UiEventItemAdapter for event → Item notification conversion
- MethodDispatch for JSON-RPC request handling
- JSON-RPC notification broadcasting (not v1 UiEventEnvelope)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


from voidx.presentation.gateway.adapter import UiEventItemAdapter
from voidx.presentation.gateway.session import GatewayEventConsumer, GatewaySession
from voidx.presentation.transcript_snapshot import TranscriptNodeRow, replace_transcript
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.schema import (
    AssistantStreamUpdated,
    RefreshRequested,
    TurnStarted,
)
from voidx.presentation.protocol.v2.envelope import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcError,
    JsonRpcResult,
    PROTOCOL_VERSION,
    parse_jsonrpc_message,
)
from voidx.presentation.protocol.v2.threads import ThreadInfo


from tests.test_presentation.gateway.helpers import FakeClient, _parse, _method, _params

# ── session CRUD methods ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_session_create_method_registers_thread(tmp_path):
    """session.create persists a session and registers a thread in-memory."""
    import voidx.persistence.sqlite as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(
        id=10, method="session.create", params={"title": "New thread"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    info = result.result
    assert info["title"] == "New thread"
    assert info["status"] == "idle"
    assert info["thread_id"] in [t.thread_id for t in session.list_threads()]
    store._conn = None




@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["chat", "coding", "loop", "goal"])
async def test_v2_session_create_persists_all_runtime_profiles(tmp_path, profile):
    """Every desktop runtime mode is accepted and survives the session round trip."""
    import voidx.persistence.sqlite as store
    from voidx.agent.adapters.persistence.session_repository import get_session

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")

    result = await session.dispatch_request(JsonRpcRequest(
        id=20, method="session.create", params={"profile": profile},
    ))

    assert isinstance(result, JsonRpcResult)
    thread_id = result.result["thread_id"]
    assert result.result["runtime_profile"] == profile
    assert (await get_session(thread_id)).runtime_profile == profile
    assert next(t for t in session.list_threads() if t.thread_id == thread_id).runtime_profile == profile
    store._conn = None




@pytest.mark.asyncio
async def test_session_persistence_rejects_unknown_runtime_profile(tmp_path):
    import voidx.persistence.sqlite as store
    from voidx.agent.adapters.persistence.session_repository import create_session

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"

    with pytest.raises(ValueError, match="unknown runtime profile: invalid"):
        await create_session(workspace=str(tmp_path), profile="invalid")
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_switch_returns_runtime_profile(tmp_path):
    import voidx.persistence.sqlite as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    created = await session.dispatch_request(JsonRpcRequest(
        id=22, method="session.create", params={"profile": "goal"},
    ))
    assert isinstance(created, JsonRpcResult)

    switched = await session.dispatch_request(JsonRpcRequest(
        id=23,
        method="session.switch",
        params={"thread_id": created.result["thread_id"]},
    ))
    assert isinstance(switched, JsonRpcResult)
    assert switched.result["runtime_profile"] == "goal"
    store._conn = None

@pytest.mark.asyncio
async def test_v2_session_create_rejects_unknown_runtime_profile(tmp_path):
    import voidx.persistence.sqlite as store
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")

    result = await session.dispatch_request(JsonRpcRequest(
        id=21, method="session.create", params={"profile": "unknown"},
    ))

    assert isinstance(result, JsonRpcError)
    assert "unknown profile" in result.error.message
    store._conn = None

@pytest.mark.asyncio
async def test_v2_session_create_with_directory(tmp_path):
    """session.create with directory param persists and returns directory."""
    import voidx.persistence.sqlite as store
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(
        id=11, method="session.create",
        params={"title": "Dir thread", "directory": "Frameworks"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    info = result.result
    assert info["directory"] == "Frameworks"
    threads = session.list_threads()
    matched = [t for t in threads if t.thread_id == info["thread_id"]]
    assert matched and matched[0].directory == "Frameworks"
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_create_defaults_empty_directory(tmp_path):
    """session.create without directory defaults to empty string."""
    import voidx.persistence.sqlite as store
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(
        id=12, method="session.create", params={"title": "Root thread"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["directory"] == ""
    store._conn = None

@pytest.mark.asyncio
async def test_v2_session_rename_method_updates_title(tmp_path):
    import voidx.persistence.sqlite as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    from voidx.agent.adapters.persistence.session_repository import create_session

    info = await create_session()

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id=info.id)

    request = JsonRpcRequest(
        id=11, method="session.rename",
        params={"thread_id": info.id, "title": "Renamed"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    thread = next(t for t in session.list_threads() if t.thread_id == info.id)
    assert thread.title == "Renamed"
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_delete_method_removes_thread(tmp_path):
    import voidx.persistence.sqlite as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    from voidx.agent.adapters.persistence.session_repository import create_session

    info = await create_session()

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id=info.id)

    request = JsonRpcRequest(
        id=12, method="session.delete", params={"thread_id": info.id},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    assert info.id not in [t.thread_id for t in session.list_threads()]
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_switch_allows_running_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second")

    # Mark t2 as running
    session._threads["t2"] = session._threads["t2"].model_copy(
        update={"status": "running"},
    )

    request = JsonRpcRequest(
        id=13, method="session.switch", params={"thread_id": "t2"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["active_thread_id"] == "t2"
    assert session.active_thread_id == "t2"


# ── diff.review.apply writes files ────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_diff_apply_writes_approved_hunks_to_file(tmp_path):
    """diff.apply should rebuild files with only approved hunks applied."""
    target = tmp_path / "sample.txt"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    # Diff: replace line2 with line2-modified
    diff_text = (
        f"--- {target}\n"
        f"+++ {target}\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-modified\n"
        " line3\n"
    )

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    review_req = JsonRpcRequest(
        id=20, method="diff.review", params={"diff": diff_text},
    )
    review_result = await session.dispatch_request(review_req)
    assert isinstance(review_result, JsonRpcResult)
    review_id = review_result.result["review_id"]

    # Approve the single hunk
    decide_req = JsonRpcRequest(
        id=21, method="diff.decide",
        params={"review_id": review_id, "file_path": str(target),
                "hunk_index": 0, "decision": "approved"},
    )
    await session.dispatch_request(decide_req)

    # Apply
    apply_req = JsonRpcRequest(
        id=22, method="diff.apply", params={"review_id": review_id},
    )
    apply_result = await session.dispatch_request(apply_req)

    assert isinstance(apply_result, JsonRpcResult)
    assert apply_result.result["files_changed"] == [str(target)]
    assert target.read_text(encoding="utf-8") == "line1\nline2-modified\nline3\n"


@pytest.mark.asyncio
async def test_v2_diff_apply_skips_rejected_hunks(tmp_path):
    """Rejected hunks must not modify the file."""
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    diff_text = (
        f"--- {target}\n"
        f"+++ {target}\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        " gamma\n"
    )

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    review_req = JsonRpcRequest(
        id=30, method="diff.review", params={"diff": diff_text},
    )
    review_result = await session.dispatch_request(review_req)
    review_id = review_result.result["review_id"]

    # Reject the hunk
    decide_req = JsonRpcRequest(
        id=31, method="diff.decide",
        params={"review_id": review_id, "file_path": str(target),
                "hunk_index": 0, "decision": "rejected"},
    )
    await session.dispatch_request(decide_req)

    apply_req = JsonRpcRequest(
        id=32, method="diff.apply", params={"review_id": review_id},
    )
    apply_result = await session.dispatch_request(apply_req)

    assert isinstance(apply_result, JsonRpcResult)
    assert apply_result.result["files_changed"] == []
    # File unchanged
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


# ── diff.generate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_diff_generate_returns_unified_diff(tmp_path):
    """diff.generate runs git diff in the workspace and returns unified diff text."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tracked.txt").write_text("line1\nline2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    request = JsonRpcRequest(id=40, method="diff.generate", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    diff_text = result.result["diff"]
    assert "+line3" in diff_text
    assert "tracked.txt" in diff_text


@pytest.mark.asyncio
async def test_v2_diff_generate_empty_repo_returns_empty_diff(tmp_path):
    """diff.generate on a clean repo returns empty diff string."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "clean.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    request = JsonRpcRequest(id=41, method="diff.generate", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["diff"] == ""


@pytest.mark.asyncio
async def test_v2_diff_generate_non_git_repo_returns_empty(tmp_path):
    """diff.generate in a non-git directory returns empty diff without error."""
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    request = JsonRpcRequest(id=42, method="diff.generate", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["diff"] == ""
