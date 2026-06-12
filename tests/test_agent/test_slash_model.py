import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.slash.runtime import _select_from_list
from voidx.agent.task_state import GoalType, PendingApproval, TaskState, goal_from_text
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


@pytest.mark.asyncio
async def test_parallel_toggle_off_persists_without_live_config_update(tmp_path, monkeypatch):
    output = _capture_handler_output(monkeypatch)
    settings = Settings(str(tmp_path))
    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))
    graph = SimpleNamespace(
        config=Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=3),
        ),
        _settings=settings,
    )

    assert await SlashHandler(graph).dispatch("/parallel off") is True

    assert graph.config.parallel_subagents == ParallelSubagentsConfig(enabled=True, max_concurrent=3)
    assert Settings(str(tmp_path)).get_parallel_subagents() == ParallelSubagentsConfig(
        enabled=False,
        max_concurrent=3,
    )
    assert output == [
        "[dim]Saved parallel subagents off (max_concurrent=3). Run /clear or restart to apply.[/dim]"
    ]


@pytest.mark.asyncio
async def test_parallel_toggle_no_arg_uses_saved_state(tmp_path, monkeypatch):
    _capture_handler_output(monkeypatch)
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/parallel") is True
    assert Settings(str(tmp_path)).get_parallel_subagents().enabled is True

    assert await handler.dispatch("/parallel") is True
    assert Settings(str(tmp_path)).get_parallel_subagents().enabled is False


@pytest.mark.asyncio
async def test_parallel_status_shows_active_and_saved_state(tmp_path, monkeypatch):
    output = _capture_handler_output(monkeypatch)
    settings = Settings(str(tmp_path))
    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
    )

    assert await SlashHandler(graph).dispatch("/parallel status") is True

    assert output == [
        "[dim]parallel subagents current off (max_concurrent=4); saved on "
        "(max_concurrent=3). Run /clear or restart to apply.[/dim]"
    ]


@pytest.mark.asyncio
async def test_parallel_invalid_arg(tmp_path, monkeypatch):
    output = _capture_handler_output(monkeypatch)
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=Settings(str(tmp_path)),
    )

    assert await SlashHandler(graph).dispatch("/parallel maybe") is True

    assert output == ["ERROR: Usage: /parallel [on|off|status]"]


@pytest.mark.asyncio
async def test_language_and_tone_dispatch_update_settings_and_live_config(tmp_path):
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
    )

    assert await SlashHandler(graph).dispatch("/lang zh-CN") is True
    assert await SlashHandler(graph).dispatch("/tone direct") is True

    assert graph.config.user_profile == UserProfile(language="zh-CN", tone="direct")
    assert Settings(str(tmp_path)).get_user_profile() == UserProfile(language="zh-CN", tone="direct")

    assert await SlashHandler(graph).dispatch("/lang auto") is True
    assert await SlashHandler(graph).dispatch("/tone default") is True

    assert graph.config.user_profile == UserProfile()
    assert Settings(str(tmp_path)).get_user_profile() == UserProfile()


@pytest.mark.asyncio
async def test_language_and_tone_without_args_use_picker(tmp_path):
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="0")
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
        _app=app,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/lang") is True
    assert app.prompt == "Language"
    assert app.choices[0][0] == "Chinese (Simplified) [zh-CN]"
    assert app.choices[-2][0] == "Other (enter manually)"
    assert app.choices[-1][0] == "Reset (auto-detect)"
    assert graph.config.user_profile.language == "zh-CN"
    assert settings.get_user_profile().language == "zh-CN"

    app.result = "3"
    assert await handler.dispatch("/tone") is True
    assert app.prompt == "Tone"
    assert app.choices[3][0] == "Direct - straightforward, no fluff"
    assert app.choices[-2][0] == "Other (enter manually)"
    assert app.choices[-1][0] == "Reset (default)"
    assert graph.config.user_profile.tone == "direct"
    assert settings.get_user_profile().tone == "direct"


@pytest.mark.asyncio
async def test_language_and_tone_picker_other_prompts_for_manual_value(tmp_path):
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="7", text_result="pt-BR")
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
        _app=app,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/lang") is True
    assert app.text_prompt == "Language code (e.g. fr, de, pt-BR; auto to reset)"
    assert graph.config.user_profile.language == "pt-BR"

    app.result = "6"
    app.text_result = "patient"
    assert await handler.dispatch("/tone") is True
    assert app.text_prompt == "Tone (e.g. patient, enthusiastic; default to reset)"
    assert graph.config.user_profile.tone == "patient"


@pytest.mark.asyncio
async def test_language_and_tone_picker_reset_clears_values(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_user_language("en")
    settings.set_user_tone("formal")
    app = FakeChoiceApp(result="8")
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=app,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/lang") is True
    assert graph.config.user_profile == UserProfile(tone="formal")
    assert settings.get_user_profile() == UserProfile(tone="formal")

    app.result = "7"
    assert await handler.dispatch("/tone") is True
    assert graph.config.user_profile == UserProfile()
    assert settings.get_user_profile() == UserProfile()


@pytest.mark.asyncio
async def test_language_and_tone_headless_fallback_prompts_for_text(tmp_path):
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
        _app=None,
    )
    handler = SlashHandler(graph)
    prompts: list[str] = []
    responses = ["ja", "technical"]

    async def fake_prompt(text: str, default: str = "", secret: bool = False) -> str:
        prompts.append(text)
        return responses.pop(0)

    handler._prompt = fake_prompt

    assert await handler.dispatch("/lang") is True
    assert await handler.dispatch("/tone") is True

    assert prompts == [
        "Language code (or 'auto' to reset)",
        "Tone (or 'default' to reset)",
    ]
    assert graph.config.user_profile == UserProfile(language="ja", tone="technical")
    assert settings.get_user_profile() == UserProfile(language="ja", tone="technical")


@pytest.mark.asyncio
async def test_language_and_tone_picker_cancel_keeps_current_values(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_user_language("en")
    settings.set_user_tone("direct")
    app = FakeChoiceApp(result=None)
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=app,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/lang") is True
    assert await handler.dispatch("/tone") is True

    assert graph.config.user_profile == UserProfile(language="en", tone="direct")
    assert settings.get_user_profile() == UserProfile(language="en", tone="direct")


@pytest.mark.asyncio
async def test_language_and_tone_headless_empty_input_cancels(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_user_language("en")
    settings.set_user_tone("direct")
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=None,
    )
    handler = SlashHandler(graph)

    async def fake_prompt(_text: str, default: str = "", secret: bool = False) -> str:
        return ""

    handler._prompt = fake_prompt

    assert await handler.dispatch("/lang") is True
    assert await handler.dispatch("/tone") is True

    assert graph.config.user_profile == UserProfile(language="en", tone="direct")
    assert settings.get_user_profile() == UserProfile(language="en", tone="direct")


def test_language_and_tone_reset_commands_are_hidden_from_palette():
    from voidx.ui.commands import COMMANDS

    names = [name for name, _description in COMMANDS]

    assert "/lang" in names
    assert "/tone" in names
    assert "/lang auto" not in names
    assert "/tone default" not in names


def test_parallel_command_is_in_palette():
    from voidx.ui.commands import COMMANDS

    assert ("/parallel", "Toggle parallel subagent execution") in COMMANDS
    assert ("/parallel on", "Enable parallel subagent execution") in COMMANDS
    assert ("/parallel off", "Disable parallel subagent execution") in COMMANDS
    assert ("/parallel status", "Show parallel subagent config") in COMMANDS


@pytest.mark.asyncio
async def test_permission_mode_without_args_uses_prompt_app_choice(tmp_path):
    settings = Settings(str(tmp_path))
    permission = PermissionService()
    app = FakeChoiceApp(result="auto-review")
    graph = SimpleNamespace(
        _permission=permission,
        _settings=settings,
        _app=app,
    )

    assert await SlashHandler(graph).dispatch("/permission-mode") is True

    assert app.prompt == "Permission mode"
    assert permission.permission_mode == "auto-review"
    assert permission.approval_policy == "untrusted"
    assert permission.approval_reviewer == "auto_review"


@pytest.mark.asyncio
async def test_mode_dispatch_updates_interaction_mode():
    graph = SimpleNamespace(
        _interaction_mode=None,
        _plan_mode=False,
        _app=None,
    )

    assert await SlashHandler(graph).dispatch("/mode goal") is True

    assert graph._interaction_mode.value == "goal"
    assert graph._plan_mode is False


@pytest.mark.asyncio
async def test_mode_dispatch_rejects_removed_modes():
    graph = SimpleNamespace(
        _interaction_mode=None,
        _plan_mode=False,
        _app=None,
    )

    assert await SlashHandler(graph).dispatch("/mode review") is True

    assert graph._interaction_mode is None
    assert graph._plan_mode is False


@pytest.mark.asyncio
async def test_code_ide_dispatch_saves_ghostty(tmp_path):
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(_settings=settings, _app=None)

    assert await SlashHandler(graph).dispatch("/code-ide ghostty") is True

    assert settings.get_code_ide() == CodeIde.GHOSTTY


@pytest.mark.asyncio
async def test_code_ide_dispatch_rejects_gostty_typo(tmp_path):
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(_settings=settings, _app=None)

    assert await SlashHandler(graph).dispatch("/code-ide gostty") is True

    assert settings.get_code_ide() == CodeIde.TRAE


@pytest.mark.asyncio
async def test_code_ide_dispatch_uses_choice_panel(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result=CodeIde.CURSOR.value)
    graph = SimpleNamespace(_settings=settings, _app=app)

    monkeypatch.setattr("voidx.agent.slash.code_ide.detect_code_ides", lambda: [])

    assert await SlashHandler(graph).dispatch("/code-ide") is True

    assert app.prompt == "Code IDE"
    assert any(choice[1] == CodeIde.GHOSTTY.value for choice in app.choices)
    assert settings.get_code_ide() == CodeIde.CURSOR


@pytest.mark.asyncio
async def test_plan_and_unplan_are_mode_aliases():
    graph = SimpleNamespace(
        _interaction_mode=None,
        _plan_mode=False,
        _app=None,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/plan") is True
    assert graph._interaction_mode.value == "plan"
    assert graph._plan_mode is True

    assert await handler.dispatch("/unplan") is True
    assert graph._interaction_mode.value == "auto"
    assert graph._plan_mode is False


@pytest.mark.asyncio
async def test_goal_dispatch_sets_goal_and_goal_mode():
    graph = SimpleNamespace(
        _interaction_mode=None,
        _plan_mode=False,
        _app=None,
        _task_state=TaskState(),
    )

    assert await SlashHandler(graph).dispatch("/goal 优化 markdown 渲染截断") is True

    assert graph._interaction_mode.value == "goal"
    assert graph._plan_mode is False
    assert graph._task_state.current_goal is not None
    assert graph._task_state.current_goal.target == "优化 markdown 渲染截断"


@pytest.mark.asyncio
async def test_goal_clear_resets_goal_and_returns_to_auto():
    state = TaskState(
        current_goal=goal_from_text("优化 markdown 渲染截断", goal_type=GoalType.DESIGN),
        pending_approval=PendingApproval(scope="优化 markdown 渲染截断"),
    )
    graph = SimpleNamespace(
        _interaction_mode=None,
        _plan_mode=False,
        _app=None,
        _task_state=state,
    )

    assert await SlashHandler(graph).dispatch("/goal clear") is True

    assert graph._interaction_mode.value == "auto"
    assert graph._task_state.current_goal is None
    assert graph._task_state.pending_approval is None
