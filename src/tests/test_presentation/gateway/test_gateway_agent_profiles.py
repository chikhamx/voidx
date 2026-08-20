from __future__ import annotations

from pathlib import Path

import pytest

from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.protocol.v2.envelope import JsonRpcError, JsonRpcRequest, JsonRpcResult


PROFILE = """\
name: rpc-reviewer
revision: 1
display_name: RPC Reviewer
identity: secret prompt text
"""

UPDATED = """\
name: rpc-reviewer
revision: 2
display_name: Updated RPC Reviewer
identity: newer secret prompt
"""


def _session(workspace: Path) -> GatewaySession:
    from voidx.bootstrap.agent_catalog import tool_catalog

    return GatewaySession(
        lambda: BottomInputDock().tree,
        workspace=str(workspace),
        agent_tool_catalog_provider=tool_catalog,
    )


async def _dispatch(session: GatewaySession, request_id: int, method: str, params: dict):
    return await session.dispatch_request(
        JsonRpcRequest(id=request_id, method=method, params=params)
    )


def _assert_public_profile(profile: dict) -> None:
    assert set(profile) == {
        "name",
        "display_name",
        "revision",
        "content_hash",
        "source",
        "run_mode",
        "hitl_mode",
        "availability",
        "diagnostics",
    }
    rendered = str(profile)
    assert "canonical_payload" not in rendered
    assert "secret prompt" not in rendered
    assert ".voidx/agents" not in rendered


@pytest.mark.asyncio
async def test_agent_profile_methods_are_registered(tmp_path: Path) -> None:
    session = _session(tmp_path)

    for method in (
        "list-agent-profiles",
        "get-agent-profile",
        "validate-agent-profile",
        "save-agent-profile",
        "delete-agent-profile",
    ):
        assert method in session.methods.registered_methods()


@pytest.mark.asyncio
async def test_validate_returns_diagnostics_without_writing(tmp_path: Path) -> None:
    session = _session(tmp_path)

    valid = await _dispatch(
        session,
        1,
        "validate-agent-profile",
        {"scope": "project", "name": "rpc-reviewer", "yaml": PROFILE},
    )
    assert isinstance(valid, JsonRpcResult)
    assert valid.result["valid"] is True
    assert valid.result["snapshot"]["profile_id"] == "rpc-reviewer"
    assert set(valid.result["snapshot"]) == {
        "profile_id", "revision", "source", "content_hash", "snapshot_hash"
    }
    assert not (tmp_path / ".voidx" / "agents").exists()

    invalid = await _dispatch(
        session,
        2,
        "validate-agent-profile",
        {"scope": "project", "name": "rpc-reviewer", "yaml": "name: wrong\nrevision: 1\n"},
    )
    assert isinstance(invalid, JsonRpcResult)
    assert invalid.result["valid"] is False
    assert invalid.result["snapshot"] is None
    assert any(item["code"] == "name_mismatch" for item in invalid.result["diagnostics"])


@pytest.mark.asyncio
async def test_save_validation_error_returns_structured_diagnostics(tmp_path: Path) -> None:
    session = _session(tmp_path)

    invalid = await _dispatch(
        session,
        3,
        "save-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "yaml": "name: wrong\nrevision: 1\n",
            "expected_revision": 0,
        },
    )

    assert isinstance(invalid, JsonRpcError)
    assert invalid.error.code == -32602
    assert invalid.error.message == "invalid agent profile"
    assert invalid.error.data is not None
    diagnostics = invalid.error.data["diagnostics"]
    assert any(item["code"] == "name_mismatch" for item in diagnostics)
    assert all(set(item) == {"path", "code", "message", "severity"} for item in diagnostics)


@pytest.mark.asyncio
async def test_save_list_and_delete_profile(tmp_path: Path) -> None:
    session = _session(tmp_path)

    saved = await _dispatch(
        session,
        1,
        "save-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "yaml": PROFILE,
            "expected_revision": 0,
        },
    )
    assert isinstance(saved, JsonRpcResult)
    assert saved.result["snapshot"]["revision"] == 1
    content_hash = saved.result["snapshot"]["content_hash"]

    listed = await _dispatch(session, 2, "list-agent-profiles", {})
    assert isinstance(listed, JsonRpcResult)
    profile = next(p for p in listed.result["profiles"] if p["name"] == "rpc-reviewer")
    _assert_public_profile(profile)
    assert profile["content_hash"] == content_hash

    deleted = await _dispatch(
        session,
        3,
        "delete-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "expected_hash": content_hash,
        },
    )
    assert isinstance(deleted, JsonRpcResult)
    assert deleted.result == {"ok": True}

    listed_after = await _dispatch(session, 4, "list-agent-profiles", {})
    assert isinstance(listed_after, JsonRpcResult)
    assert not any(p["name"] == "rpc-reviewer" for p in listed_after.result["profiles"])


@pytest.mark.asyncio
async def test_save_conflict_has_stable_code_and_safe_current_metadata(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await _dispatch(
        session,
        1,
        "save-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "yaml": PROFILE,
            "expected_revision": 0,
        },
    )

    conflict = await _dispatch(
        session,
        2,
        "save-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "yaml": UPDATED,
            "expected_revision": 0,
        },
    )

    assert isinstance(conflict, JsonRpcError)
    assert conflict.error.code == -32010
    assert conflict.error.message == "agent profile conflict"
    assert conflict.error.data is not None
    _assert_public_profile(conflict.error.data["current"])


@pytest.mark.asyncio
async def test_bundled_delete_is_read_only_and_params_are_strict(tmp_path: Path) -> None:
    session = _session(tmp_path)

    read_only = await _dispatch(
        session,
        1,
        "delete-agent-profile",
        {"scope": "bundled", "name": "coding", "expected_revision": 1},
    )
    assert isinstance(read_only, JsonRpcError)
    assert read_only.error.code == -32011
    assert read_only.error.message == "agent profile is read-only"

    save_read_only = await _dispatch(
        session,
        2,
        "save-agent-profile",
        {
            "scope": "bundled",
            "name": "coding",
            "yaml": "name: coding\nrevision: 2\n",
            "expected_revision": 1,
        },
    )
    assert isinstance(save_read_only, JsonRpcError)
    assert save_read_only.error.code == -32011
    assert save_read_only.error.message == "agent profile is read-only"

    bad_params = await _dispatch(
        session,
        2,
        "save-agent-profile",
        {"scope": "project", "name": "../escape", "yaml": PROFILE, "expected_revision": 0},
    )
    assert isinstance(bad_params, JsonRpcError)
    assert bad_params.error.code == -32602


@pytest.mark.asyncio
async def test_internal_filesystem_error_is_not_exposed_to_rpc_client(
    tmp_path: Path, monkeypatch
) -> None:
    import voidx.presentation.gateway.session.method.agent_profiles as methods

    secret = str(tmp_path / ".voidx" / "agents" / "rpc-reviewer.yaml")

    def fail_save(*args, **kwargs):
        raise PermissionError(f"permission denied: {secret}")

    monkeypatch.setattr(methods, "save_agent_profile", fail_save)
    result = await _dispatch(
        _session(tmp_path),
        99,
        "save-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "yaml": PROFILE,
            "expected_revision": 0,
        },
    )

    assert isinstance(result, JsonRpcError)
    assert result.error.code == -32603
    assert result.error.message == "internal error"
    assert result.error.data is None
    assert secret not in result.model_dump_json()
    assert "permission denied" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_get_agent_profile_returns_canonical_yaml_without_path(tmp_path: Path) -> None:
    session = _session(tmp_path)
    saved = await _dispatch(
        session,
        120,
        "save-agent-profile",
        {
            "scope": "project",
            "name": "rpc-reviewer",
            "yaml": PROFILE,
            "expected_revision": 0,
        },
    )
    assert isinstance(saved, JsonRpcResult)

    result = await _dispatch(
        session,
        121,
        "get-agent-profile",
        {"scope": "project", "name": "rpc-reviewer"},
    )

    assert isinstance(result, JsonRpcResult)
    assert result.result["profile"]["name"] == "rpc-reviewer"
    assert result.result["profile"]["source"] == "project"
    assert result.result["read_only"] is False
    assert result.result["yaml"].startswith("name: rpc-reviewer\nrevision: 1\n")
    rendered = result.model_dump_json()
    assert str(tmp_path) not in rendered
    assert "canonical_payload" not in rendered


@pytest.mark.asyncio
async def test_get_agent_profile_unknown_is_stable_not_found(tmp_path: Path) -> None:
    result = await _dispatch(
        _session(tmp_path),
        122,
        "get-agent-profile",
        {"scope": "project", "name": "missing"},
    )

    assert isinstance(result, JsonRpcError)
    assert result.error.code == -32012
    assert result.error.message == "agent profile not found"
    assert result.error.data is None


@pytest.mark.asyncio
async def test_agent_catalog_method_returns_aggregated_metadata(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert "agent-catalog" in session.methods.registered_methods()

    result = await _dispatch(session, 90, "agent-catalog", {})
    assert isinstance(result, JsonRpcResult)
    payload = result.result

    tools = payload["tools"]
    tool_ids = {tool["id"] for tool in tools}
    assert {"read", "bash", "todo", "clarify"} <= tool_ids
    for tool in tools:
        assert set(tool) == {"id", "description"}

    node_names = {node["name"] for node in payload["builtin_nodes"]}
    assert node_names == {
        "brainstorm", "design", "plan", "tdd", "verify", "review", "feedback", "debug",
    }
    edges = payload["default_edges"]
    assert edges
    for edge in edges:
        assert edge["source"] in node_names
        assert edge["target"] in node_names
        assert edge["condition"].strip()

    assert isinstance(payload["skills"], list)
    assert isinstance(payload["mcp_servers"], list)
