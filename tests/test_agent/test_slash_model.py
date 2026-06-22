import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.slash.runtime import _select_from_list
from voidx.agent.task_state import GoalSpec, TaskState
from voidx.config import (
    CodeIde,
    ApprovalPolicy,
    ApprovalReviewer,
    Config,
    McpServerConfig,
    ModelConfig,
    ParallelSubagentsConfig,
    PermissionMode,
    SandboxMode,
    Settings,
    UserProfile,
)
from voidx.permission.service import PermissionService
from voidx.llm.catalog import STATIC_MODELS
from voidx.llm.usage import UsageStats
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


@pytest.mark.asyncio
async def test_select_from_list_uses_prompt_app_choice():
    app = FakeChoiceApp(result="1")

    selected = await _select_from_list(app, "Provider", ["anthropic", "mimo"])

    assert selected == 1
    assert app.prompt == "Provider"
    assert app.choices == [
        ("anthropic", "0", ""),
        ("mimo", "1", ""),
    ]


@pytest.mark.asyncio
async def test_model_list_reads_settings_profiles(tmp_path):
    profile_one = f"mimo/{tmp_path.name}-v2.5"
    profile_two = f"openai/{tmp_path.name}-gpt-4o"
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "default_profile": profile_one,
            "profiles": {
                profile_one: {"api_key": "sk-test-1"},
                profile_two: {"api_key": "sk-test-2"},
            },
        }),
        encoding="utf-8",
    )
    try:
        settings = await Settings.create(str(tmp_path))
        graph = SimpleNamespace(
            config=await settings.build_config(),
            _settings=settings,
            model=object(),
            _app=None,
        )

        await SlashHandler(graph)._model_list()
    finally:
        await delete_model_profile_async(profile_one)
        await delete_model_profile_async(profile_two)


@pytest.mark.asyncio
async def test_model_test_dispatch_strips_command_prefix():
    graph = SimpleNamespace()
    handler = SlashHandler(graph)
    targets: list[str] = []

    async def fake_model_test(target: str) -> None:
        targets.append(target)

    handler._model_test = fake_model_test

    assert await handler.dispatch("/model test") is True
    assert await handler.dispatch("/model test mimo/mimo-v2.5") is True

    assert targets == ["", "mimo/mimo-v2.5"]


@pytest.mark.asyncio
async def test_slash_dispatch_rejects_unknown_prefix_command():
    assert await SlashHandler(SimpleNamespace()).dispatch("/debugx") is False


@pytest.mark.asyncio
async def test_model_test_creates_model_with_config_defaults(tmp_path, monkeypatch):
    profile_name = f"openai/{tmp_path.name}-gpt-5.4-mini"
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "default_profile": profile_name,
            "profiles": {
                profile_name: {
                    "api_key": "sk-test",
                },
            },
        }),
        encoding="utf-8",
    )
    try:
        settings = await Settings.create(str(tmp_path))
        graph = SimpleNamespace(
            config=await settings.build_config(),
            _settings=settings,
            _app=None,
        )
        captured = {}

        def fake_create_chat_model(api_key, cfg):
            captured["reasoning_effort"] = cfg.reasoning_effort
            return object()

        monkeypatch.setattr("voidx.llm.provider.create_chat_model", fake_create_chat_model)
        handler = SlashHandler(graph)

        async def fake_test_connection(_model):
            return True, ""

        handler._test_connection = fake_test_connection

        await handler._model_test(profile_name)

        assert captured["reasoning_effort"] == "xhigh"
    finally:
        await delete_model_profile_async(profile_name)


@pytest.mark.asyncio
async def test_model_prompt_uses_prompt_app_text_input():
    app = FakeChoiceApp(result="sk-test")
    graph = SimpleNamespace(_app=app)

    result = await SlashHandler(graph)._prompt("API key", secret=True)

    assert result == "sk-test"
    assert app.text_prompt == "API key"
    assert app.text_secret is True


@pytest.mark.asyncio
async def test_tavily_set_creates_mcp_server_and_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    manager = SimpleNamespace(restarts=0)

    async def restart_all():
        manager.restarts += 1

    manager.restart_all = restart_all
    graph = SimpleNamespace(_settings=settings, _app=app, _mcp_manager=manager)

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
    graph = SimpleNamespace(_settings=settings, _app=app)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    tavily = settings.get_mcp_server("tavily")
    assert tavily.command == "custom"
    assert tavily.args == ["serve"]
    assert tavily.disabled is True
    assert tavily.tools == ["custom_tool"]
    assert tavily.env == {"OTHER": "1", "TAVILY_API_KEY": "tvly-new"}


@pytest.mark.asyncio
async def test_tavily_set_restarts_mcp_manager_when_available(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    manager = SimpleNamespace(restarts=0)

    async def restart_all():
        manager.restarts += 1

    manager.restart_all = restart_all
    graph = SimpleNamespace(_settings=settings, _app=app, _mcp_manager=manager)

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
    graph = SimpleNamespace(_settings=settings, _app=None)

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
    graph = SimpleNamespace(_settings=settings, _app=app)

    handled = await SlashHandler(graph).dispatch("/tavily set tvly-plain")

    assert handled is True
    assert settings.get_tavily_api_key() is None
    assert app.text_prompt == ""


@pytest.mark.asyncio
async def test_tavily_mcp_restart_skips_when_manager_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    graph = SimpleNamespace(_settings=settings, _app=app, _mcp_manager=None)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    assert settings.get_tavily_api_key() == "tvly-secret"
    assert settings.get_mcp_server("tavily") is not None


def test_model_provider_list_matches_catalog():
    from voidx.agent.slash.runtime import PROVIDERS

    assert set(STATIC_MODELS).issubset(PROVIDERS)


def test_slash_handler_uses_runtime_ui_singleton():
    from voidx.agent.slash.handler import ui as slash_ui
    from voidx.runtime.ui import ui as runtime_ui

    assert slash_ui is runtime_ui


def test_model_status_sync_updates_prompt_footer_state():
    status = SimpleNamespace(
        provider="old",
        model="old",
        reasoning_effort="medium",
        context_limit=0,
    )
    graph = SimpleNamespace(
        config=SimpleNamespace(
            model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high")
        ),
        _usage_stats=UsageStats(),
        _app=SimpleNamespace(status=status),
    )

    SlashHandler(graph)._sync_context_limit()

    assert status.provider == "mimo"
    assert status.model == "mimo-v2.5"
    assert status.reasoning_effort == "high"
    assert status.context_limit == 1_000_000


@pytest.mark.asyncio
async def test_model_dispatch_without_args_opens_switch_picker():
    graph = SimpleNamespace()
    handler = SlashHandler(graph)
    targets: list[str] = []

    async def fake_model_switch(target: str) -> None:
        targets.append(target)

    handler._model_switch = fake_model_switch

    assert await handler.dispatch("/model") is True

    assert targets == [""]


@pytest.mark.asyncio
async def test_model_new_and_del_dispatch_to_matching_methods():
    graph = SimpleNamespace()
    handler = SlashHandler(graph)
    calls: list[tuple[str, str]] = []

    async def fake_model_new() -> None:
        calls.append(("new", ""))

    async def fake_model_del(target: str) -> None:
        calls.append(("del", target))

    handler._model_new = fake_model_new
    handler._model_del = fake_model_del

    assert await handler.dispatch("/model new") is True
    assert await handler.dispatch("/model del mimo/mimo-v2.5") is True

    assert calls == [("new", ""), ("del", "mimo/mimo-v2.5")]


@pytest.mark.asyncio
async def test_paste_dispatch_uses_prompt_app():
    class FakeApp:
        def __init__(self) -> None:
            self.called = False

        def paste_clipboard_image(self):
            self.called = True
            return ClipboardImageResult(
                status="ok",
                message="Pasted image",
                rel_path=".voidx/attachments/clip.png",
            )

    app = FakeApp()
    graph = SimpleNamespace(_app=app)

    assert await SlashHandler(graph).dispatch("/paste") is True
    assert app.called is True


@pytest.mark.asyncio
async def test_usage_dispatch_reads_usage_stats():
    graph = SimpleNamespace(
        _usage_stats=UsageStats(
            context_tokens=100,
            context_limit=1_000,
            last_input_tokens=100,
            last_output_tokens=20,
            total_input_tokens=200,
            total_output_tokens=40,
            total_calls=2,
        )
    )

    assert await SlashHandler(graph).dispatch("/usage") is True


@pytest.mark.asyncio
async def test_permission_mode_dispatch_updates_service_and_settings(tmp_path):
    settings = Settings(str(tmp_path))
    permission = PermissionService()
    graph = SimpleNamespace(
        _permission=permission,
        _settings=settings,
        _app=None,
    )

    assert await SlashHandler(graph).dispatch("/permission-mode full-access") is True

    reloaded = await Settings.create(str(tmp_path))
    cfg = await reloaded.build_config()
    assert permission.permission_mode == "full-access"
    assert permission.sandbox_mode == "danger-full-access"
    assert permission.approval_policy == "never"
    assert cfg.permission_mode == PermissionMode.FULL_ACCESS
    assert cfg.sandbox_mode == SandboxMode.DANGER_FULL_ACCESS
    assert cfg.approval_policy == ApprovalPolicy.NEVER
    assert cfg.approval_reviewer == ApprovalReviewer.USER


@pytest.mark.asyncio
async def test_parallel_toggle_on_persists_without_live_config_update(tmp_path, monkeypatch):
    output = _capture_handler_output(monkeypatch)
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
    )

    assert await SlashHandler(graph).dispatch("/parallel on") is True

    assert graph.config.parallel_subagents.enabled is False
    assert Settings(str(tmp_path)).get_parallel_subagents() == ParallelSubagentsConfig(enabled=True)
    assert output == [
        "[dim]Saved parallel subagents on (max_concurrent=4). Run /clear or restart to apply.[/dim]"
    ]


