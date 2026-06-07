import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.config import McpServerConfig, Settings, WebToolRoute
from voidx.mcp.schema import McpToolDef
from voidx.ui.commands import filter_commands


class FakePromptApp:
    def __init__(self, choices=None, texts=None) -> None:
        self.choices = list(choices or [])
        self.texts = list(texts or [])
        self.choice_prompts = []
        self.text_prompts = []

    async def ask_choice(self, prompt, choices, details=None):
        self.choice_prompts.append(prompt)
        return self.choices.pop(0)

    async def ask_text(self, prompt, default="", secret=False):
        self.text_prompts.append(prompt)
        if self.texts:
            return self.texts.pop(0)
        return default


class FakeMcpManager:
    def __init__(self) -> None:
        self.restarts = 0
        self.started = True

    async def restart_all(self) -> None:
        self.restarts += 1

    def statuses(self):
        return []


def test_mcp_enable_disable_commands_are_in_palette():
    assert ("/mcp disable", "Disable an MCP server") in filter_commands("/mcp d")
    assert ("/mcp enable", "Enable an MCP server") in filter_commands("/mcp e")


@pytest.mark.asyncio
async def test_mcp_new_builtin_saves_server_and_web_routes(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path))
    app = FakePromptApp(choices=["0"], texts=["voidx-web"])
    manager = FakeMcpManager()
    graph = SimpleNamespace(_settings=settings, _app=app, _mcp_manager=manager)
    handler = SlashHandler(graph)

    async def fake_test(server):
        assert server.name == "voidx-web"
        return True, [McpToolDef(name="web_search"), McpToolDef(name="web_fetch")], ""

    monkeypatch.setattr(handler, "_test_mcp_config", fake_test)

    await handler.dispatch("/mcp new")

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["voidx-web"]["args"] == ["-m", "voidx.mcp_servers.web"]
    assert saved["mcpServers"]["voidx-web"]["tools"] == ["web_search", "web_fetch"]
    assert saved["web"]["search"] == {
        "backend": "mcp",
        "server": "voidx-web",
        "tool": "web_search",
    }
    assert saved["web"]["fetch"] == {
        "backend": "mcp",
        "server": "voidx-web",
        "tool": "web_fetch",
    }
    assert manager.restarts == 1


@pytest.mark.asyncio
async def test_mcp_new_tavily_saves_server_and_web_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakePromptApp(choices=["1"], texts=["tavily", "tvly-test"])
    manager = FakeMcpManager()
    graph = SimpleNamespace(_settings=settings, _app=app, _mcp_manager=manager)
    handler = SlashHandler(graph)

    async def fake_test(server):
        assert server.name == "tavily"
        assert server.command == "npx"
        assert server.args == ["-y", "tavily-mcp@latest"]
        assert server.env == {"TAVILY_API_KEY": "tvly-test"}
        return True, [McpToolDef(name="tavily_search"), McpToolDef(name="tavily_extract")], ""

    monkeypatch.setattr(handler, "_test_mcp_config", fake_test)

    await handler.dispatch("/mcp new")

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["tavily"]["command"] == "npx"
    assert saved["mcpServers"]["tavily"]["args"] == ["-y", "tavily-mcp@latest"]
    assert saved["mcpServers"]["tavily"]["env"] == {"TAVILY_API_KEY": "tvly-test"}
    assert saved["mcpServers"]["tavily"]["tools"] == ["tavily_search", "tavily_extract"]
    assert saved["web"]["search"] == {
        "backend": "mcp",
        "server": "tavily",
        "tool": "tavily_search",
    }
    assert saved["web"]["fetch"] == {
        "backend": "mcp",
        "server": "tavily",
        "tool": "tavily_extract",
    }
    assert manager.restarts == 1


@pytest.mark.asyncio
async def test_mcp_new_url_saves_server_with_url(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path))
    app = FakePromptApp(choices=["2", "0"], texts=["my-remote", "https://mcp.example.com/sse", ""])
    manager = FakeMcpManager()
    graph = SimpleNamespace(_settings=settings, _app=app, _mcp_manager=manager)
    handler = SlashHandler(graph)

    async def fake_test(server):
        assert server.name == "my-remote"
        assert server.url == "https://mcp.example.com/sse"
        assert server.effective_transport == "sse"
        return True, [McpToolDef(name="remote_tool")], ""

    monkeypatch.setattr(handler, "_test_mcp_config", fake_test)

    await handler.dispatch("/mcp new")

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["my-remote"]["url"] == "https://mcp.example.com/sse"
    assert saved["mcpServers"]["my-remote"]["tools"] == ["remote_tool"]
    assert manager.restarts == 1


@pytest.mark.asyncio
async def test_mcp_disable_command_sets_disabled_true_and_restarts(tmp_path):
    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(
        name="voidx-web",
        command=sys.executable,
        args=["-m", "voidx.mcp_servers.web"],
        tools=["web_search"],
    ))
    settings.set_web_tool_route(
        "search",
        WebToolRoute(backend="mcp", server="voidx-web", tool="web_search"),
    )
    manager = FakeMcpManager()
    graph = SimpleNamespace(_settings=settings, _app=None, _mcp_manager=manager)

    await SlashHandler(graph).dispatch("/mcp disable voidx-web")

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["voidx-web"]["disabled"] is True
    assert saved["web"]["search"]["backend"] == "legacy"
    assert manager.restarts == 1


@pytest.mark.asyncio
async def test_mcp_enable_command_sets_disabled_false_and_restarts(tmp_path):
    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(
        name="voidx-web",
        command=sys.executable,
        args=["-m", "voidx.mcp_servers.web"],
        disabled=True,
        tools=["web_search"],
    ))
    manager = FakeMcpManager()
    graph = SimpleNamespace(_settings=settings, _app=None, _mcp_manager=manager)

    await SlashHandler(graph).dispatch("/mcp enable voidx-web")

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["voidx-web"]["disabled"] is False
    assert manager.restarts == 1
