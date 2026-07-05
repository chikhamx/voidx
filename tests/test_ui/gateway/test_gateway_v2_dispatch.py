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

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.gateway.adapter import UiEventItemAdapter
from voidx.ui.gateway.session import GatewayEventConsumer, GatewaySession
from voidx.memory.transcript import TranscriptNodeRow, replace_transcript
from voidx.ui.output.dock import BottomInputDock
from voidx.ui.output.events.schema import (
    AssistantStreamUpdated,
    RefreshRequested,
    TurnStarted,
)
from voidx.ui.protocol.v2.envelope import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
    parse_jsonrpc_message,
)
from voidx.ui.protocol.v2.threads import ThreadInfo
from voidx.ui.protocol.requests import UiChoiceRequest, UiResponse


from tests.test_ui.gateway.helpers import FakeClient, _parse, _method, _params

# ── JSON-RPC method dispatch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_dispatches_session_submit_method():
    dock = BottomInputDock()
    received: list[str] = []

    async def handle_submit(params):
        received.append(params["text"])
        return {"ok": True}

    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    session.methods.register("session.submit", handle_submit)
    client = FakeClient()
    await session.connect(client)

    request = JsonRpcRequest(id=1, method="session.submit", params={"text": "hello web"})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.id == 1
    assert result.result == {"ok": True}
    assert received == ["hello web"]


@pytest.mark.asyncio
async def test_session_respond_resolves_pending_ui_request():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    pending = asyncio.create_task(
        session.request(
            UiChoiceRequest(
                request_id="choice_1",
                prompt="Pick one",
                choices=[("First", "first", "First option")],
            ),
        ),
    )
    await asyncio.sleep(0)

    result = await session.dispatch_request(
        JsonRpcRequest(
            id=2,
            method="session.respond",
            params={"request_id": "choice_1", "value": "first"},
        )
    )

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    assert await asyncio.wait_for(pending, timeout=1) == UiResponse(
        request_id="choice_1",
        value="first",
    )




@pytest.mark.asyncio
async def test_commands_list_returns_desktop_catalog_metadata():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(id=11, method="commands.list", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    commands = result.result["commands"]
    model_new = next(item for item in commands if item["command"] == "/model new")
    assert model_new["category"] == "model"
    assert model_new["execution"] == "open-ui"
    assert model_new["uiTarget"] == "settings:model"
    assert model_new["requiresArgs"] is False
    assert model_new["dangerous"] is False
    rollback = next(item for item in commands if item["command"] == "/rollback")
    assert rollback["category"] == "maintenance"
    assert rollback["dangerous"] is True


@pytest.mark.asyncio
async def test_commands_run_validates_open_ui_fill_and_dangerous_commands():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    open_ui = await session.dispatch_request(JsonRpcRequest(id=12, method="commands.run", params={"text": "/model new"}))
    assert isinstance(open_ui, JsonRpcResult)
    assert open_ui.result == {"ok": True, "action": "open-ui", "uiTarget": "settings:model"}

    fill_result = await session.dispatch_request(JsonRpcRequest(id=13, method="commands.run", params={"text": "/model switch"}))
    assert hasattr(fill_result, "error")
    assert fill_result.error.message == "command requires arguments"

    dangerous_result = await session.dispatch_request(JsonRpcRequest(id=14, method="commands.run", params={"text": "/rollback"}))
    assert hasattr(dangerous_result, "error")
    assert dangerous_result.error.message == "confirmation required"
@pytest.mark.asyncio
async def test_settings_get_returns_desktop_settings_snapshot(tmp_path):
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    result = await session.dispatch_request(JsonRpcRequest(id=15, method="settings.get", params={}))

    assert isinstance(result, JsonRpcResult)
    settings = result.result
    assert settings["model"]["provider"]
    assert settings["permissions"]["permission_mode"] == "default"
    assert settings["permissions"]["sandbox_mode"] == "workspace-write"
    assert settings["permissions"]["approval_policy"] == "untrusted"
    assert settings["user_profile"] == {"language": "", "tone": ""}
    assert settings["code_ide"]
    assert settings["update_check"]["enabled"] is True
    assert settings["parallel_subagents"]["max_concurrent"] == 4
    assert settings["paths"]["workspace_settings"].endswith(".voidx/settings.json")


@pytest.mark.asyncio
async def test_settings_update_persists_preferences_and_permissions(tmp_path):
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    result = await session.dispatch_request(JsonRpcRequest(id=16, method="settings.update", params={
        "patch": {
            "permissions": {
                "permission_mode": "read-only",
                "sandbox_mode": "read-only",
                "approval_policy": "on-request",
            },
            "user_profile": {"language": "zh-CN", "tone": "concise"},
            "parallel_subagents": {"enabled": True, "max_concurrent": 3},
            "update_check": {"enabled": False},
        }
    }))

    assert isinstance(result, JsonRpcResult)
    assert result.result["ok"] is True
    settings = result.result["settings"]
    assert settings["permissions"]["permission_mode"] == "custom"
    assert settings["permissions"]["sandbox_mode"] == "read-only"
    assert settings["permissions"]["approval_policy"] == "on-request"
    assert settings["user_profile"] == {"language": "zh-CN", "tone": "concise"}
    assert settings["parallel_subagents"] == {"enabled": True, "max_concurrent": 3}
    assert settings["update_check"]["enabled"] is False


@pytest.mark.asyncio
async def test_settings_update_adds_configured_model_and_preserves_profile_fields(tmp_path):
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    result = await session.dispatch_request(JsonRpcRequest(id=17, method="settings.update", params={
        "patch": {
            "model": {
                "provider": "xunfei-coding-plan",
                "model": "astron-code-latest",
                "base_url": "https://spark-api-open.xf-yun.com/v1",
                "protocol": "openai",
            },
            "provider_secrets": {
                "provider": "xunfei-coding-plan",
                "profile_name": "xunfei-coding-plan/astron-code-latest",
                "action": "set",
                "api_key": "sk-test",
            },
        }
    }))

    assert isinstance(result, JsonRpcResult)
    profile = next(
        item for item in result.result["settings"]["profiles"]
        if item["name"] == "xunfei-coding-plan/astron-code-latest"
    )
    assert profile["configured"] is True
    assert profile["base_url"] == "https://spark-api-open.xf-yun.com/v1"
    assert profile["protocol"] == "openai"

    result = await session.dispatch_request(JsonRpcRequest(id=18, method="settings.update", params={
        "patch": {
            "model": {
                "provider": "xunfei-coding-plan",
                "model": "astron-code-latest",
            },
        }
    }))

    profile = next(
        item for item in result.result["settings"]["profiles"]
        if item["name"] == "xunfei-coding-plan/astron-code-latest"
    )
    assert profile["configured"] is True
    assert profile["base_url"] == "https://spark-api-open.xf-yun.com/v1"
    assert profile["protocol"] == "openai"



@pytest.mark.asyncio
async def test_integrations_get_returns_snapshot_sections(tmp_path, monkeypatch):
    from voidx.config.models import McpServerConfig
    from voidx.config.settings import Settings

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(name="demo", command="node", args=["server.js"], tools=["alpha"]))
    skill_path = tmp_path / ".voidx" / "skills" / "demo-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: demo-skill\ndescription: Demo skill\nenabled: true\n---\n\nBody", encoding="utf-8")

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))
    result = await session.dispatch_request(JsonRpcRequest(id=17, method="integrations.get", params={}))

    assert isinstance(result, JsonRpcResult)
    snapshot = result.result
    assert snapshot["mcp_servers"][0]["name"] == "demo"
    assert snapshot["mcp_servers"][0]["tool_count"] == 1
    assert snapshot["web_routes"]["search"]["backend"] == "legacy"
    assert snapshot["tavily"] == {"configured": False, "source": "none"}
    assert any(item["name"] == "demo-skill" for item in snapshot["skills"])
    assert isinstance(snapshot["lsp"], list)


@pytest.mark.asyncio
async def test_mcp_and_skills_row_actions_update_state(tmp_path):
    from voidx.config.models import McpServerConfig
    from voidx.config.settings import Settings

    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(name="demo", command="node", tools=["alpha"]))
    skill_path = tmp_path / ".voidx" / "skills" / "demo-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: demo-skill\ndescription: Demo skill\nenabled: true\n---\n\nBody", encoding="utf-8")

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    disabled = await session.dispatch_request(JsonRpcRequest(id=18, method="mcp.setDisabled", params={"name": "demo", "disabled": True}))
    assert isinstance(disabled, JsonRpcResult)
    assert disabled.result["server"]["disabled"] is True

    tools = await session.dispatch_request(JsonRpcRequest(id=19, method="mcp.tools", params={"name": "demo"}))
    assert isinstance(tools, JsonRpcResult)
    assert tools.result["tools"] == [{"name": "alpha", "description": ""}]

    skills = await session.dispatch_request(JsonRpcRequest(id=20, method="skills.setEnabled", params={"name": "demo-skill", "enabled": False}))
    assert isinstance(skills, JsonRpcResult)
    demo_skill = next(item for item in skills.result["skills"] if item["name"] == "demo-skill")
    assert demo_skill["enabled"] is False


@pytest.mark.asyncio
async def test_v2_dispatch_returns_method_not_found_for_unknown_method():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(id=2, method="nonexistent.method", params={})
    result = await session.dispatch_request(request)

    # dispatch_request returns JsonRpcResult | JsonRpcError
    assert result.id == 2
    # It should be an error (method not found)
    assert hasattr(result, "error")
    assert result.error.code == -32601

