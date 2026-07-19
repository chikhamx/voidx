import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from tests.test_agent.slash.context import command_context
from voidx.agent.slash.runtime import _select_from_list
from voidx.runtime.task_state import GoalSpec, TaskState
from voidx.config import (
    CodeIde,
    Config,
    McpServerConfig,
    ModelConfig,
    ParallelSubagentsConfig,
    Settings,
    UserProfile,
)
from voidx.permission.service import PermissionService
from voidx.llm.catalog import STATIC_MODELS
from voidx.llm.usage import UsageStats
from voidx.memory.model_profiles import ModelProfileRow, save_model_profile_async
from voidx.memory.model_profiles import delete_model_profile_async
from voidx.ui.tools.clipboard_image import ClipboardImageResult


class FakeChoiceApp:
    def __init__(self, result: str | None = None, text_result: str | None = None) -> None:
        self.result = result
        self.text_result = text_result
        self.prompt = ""
        self.choices = []
        self.text_prompt = ""
        self.text_secret = False

    async def ask_choice(self, prompt, choices, details=None):
        self.prompt = prompt
        self.choices = choices
        return self.result

    async def ask_text(self, prompt, default="", secret=False):
        self.text_prompt = prompt
        self.text_secret = secret
        if self.text_result is not None:
            return self.text_result
        return self.result


def _capture_handler_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.agent.slash.handler.ui.print",
        lambda text="": output.append(str(text)),
    )
    monkeypatch.setattr(
        "voidx.agent.slash.handler.ui.error",
        lambda text="": output.append(f"ERROR: {text}"),
    )
    return output



async def test_tavily_set_creates_mcp_server_and_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    manager = SimpleNamespace(restarts=0)

    async def restart_all():
        manager.restarts += 1

    manager.restart_all = restart_all
    graph = command_context(settings=settings, app=app, mcp_manager=manager)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    assert settings.get_tavily_api_key() == "tvly-secret"
    assert app.text_prompt == "Tavily API key"
    assert app.text_secret is True
    tavily = settings.get_mcp_server("tavily")
    assert tavily.command == "npx"
    assert tavily.args == ["-y", "tavily-mcp@latest"]
    assert tavily.env == {"TAVILY_API_KEY": "tvly-secret"}
    assert tavily.tools == ["tavily_search", "tavily_extract"]
    assert settings.get_web_tool_route("search").server == "tavily"
    assert settings.get_web_tool_route("search").tool == "tavily_search"
    assert settings.get_web_tool_route("fetch").tool == "tavily_extract"
    assert manager.restarts == 1


@pytest.mark.asyncio
async def test_tavily_set_updates_existing_mcp_server_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(
        name="tavily",
        command="custom",
        args=["serve"],
        env={"OTHER": "1"},
        disabled=True,
        tools=["custom_tool"],
    ))
    app = FakeChoiceApp(result="tvly-new")
    graph = command_context(settings=settings, app=app)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    tavily = settings.get_mcp_server("tavily")
    assert tavily.command == "custom"
    assert tavily.args == ["serve"]
    assert tavily.disabled is True
    assert tavily.tools == ["custom_tool"]
    assert tavily.env == {"OTHER": "1", "TAVILY_API_KEY": "tvly-new"}


@pytest.mark.asyncio
async def test_tavily_set_restartsmcp_manager_when_available(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    manager = SimpleNamespace(restarts=0)

    async def restart_all():
        manager.restarts += 1

    manager.restart_all = restart_all
    graph = command_context(settings=settings, app=app, mcp_manager=manager)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    assert manager.restarts == 1


@pytest.mark.asyncio
async def test_tavily_delete_removes_key_from_mcp_server_env_and_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    settings.set_tavily_api_key("tvly-old")
    settings.save_mcp_server(McpServerConfig(
        name="tavily",
        command="npx",
        args=["-y", "tavily-mcp@latest"],
        env={"TAVILY_API_KEY": "tvly-old", "OTHER": "1"},
        tools=["tavily_search", "tavily_extract"],
    ))
    from voidx.config import WebToolRoute
    settings.set_web_tool_route("search", WebToolRoute(backend="mcp", server="tavily", tool="tavily_search"))
    settings.set_web_tool_route("fetch", WebToolRoute(backend="mcp", server="tavily", tool="tavily_extract"))
    graph = command_context(settings=settings, app=None)

    handled = await SlashHandler(graph).dispatch("/tavily delete")

    assert handled is True
    assert settings.get_tavily_api_key() is None
    tavily = settings.get_mcp_server("tavily")
    assert tavily.env == {"OTHER": "1"}
    assert settings.get_web_tool_route("search").backend == "legacy"
    assert settings.get_web_tool_route("fetch").backend == "legacy"


@pytest.mark.asyncio
async def test_tavily_set_rejects_key_in_command_text(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    graph = command_context(settings=settings, app=app)

    handled = await SlashHandler(graph).dispatch("/tavily set tvly-plain")

    assert handled is True
    assert settings.get_tavily_api_key() is None
    assert app.text_prompt == ""


@pytest.mark.asyncio
async def test_tavily_mcp_restart_skips_when_manager_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    graph = command_context(settings=settings, app=app, mcp_manager=None)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    assert settings.get_tavily_api_key() == "tvly-secret"
    assert settings.get_mcp_server("tavily") is not None


