import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.slash.runtime import _select_from_list
from voidx.agent.task_state import PendingApproval, TaskRun, TaskState
from voidx.config import CodeIde, ApprovalPolicy, ApprovalReviewer, Config, ModelConfig, PermissionMode, SandboxMode, Settings, UserProfile
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
async def test_tavily_set_uses_secret_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result="tvly-secret")
    graph = SimpleNamespace(_settings=settings, _app=app)

    handled = await SlashHandler(graph).dispatch("/tavily set")

    assert handled is True
    assert settings.get_tavily_api_key() == "tvly-secret"
    assert app.text_prompt == "Tavily API key"
    assert app.text_secret is True


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


def test_model_provider_list_matches_catalog():
    from voidx.agent.slash.runtime import PROVIDERS

    assert set(STATIC_MODELS).issubset(PROVIDERS)


def test_slash_runtime_uses_graph_console_singleton():
    from voidx.agent.graph.runtime import ui as graph_ui
    from voidx.agent.slash.runtime import ui as slash_ui

    assert slash_ui is graph_ui


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
        _task_run=TaskRun(),
    )

    assert await SlashHandler(graph).dispatch("/goal 优化 markdown 渲染截断") is True

    assert graph._interaction_mode.value == "goal"
    assert graph._plan_mode is False
    assert graph._task_run.goal == "优化 markdown 渲染截断"
    assert graph._task_run.status.value == "active"


@pytest.mark.asyncio
async def test_goal_clear_resets_goal_and_returns_to_auto():
    run = TaskRun()
    run.set_goal("优化 markdown 渲染截断")
    state = TaskState(pending_approval=PendingApproval(scope="优化 markdown 渲染截断"))
    graph = SimpleNamespace(
        _interaction_mode=None,
        _plan_mode=False,
        _app=None,
        _task_run=run,
        _task_state=state,
    )

    assert await SlashHandler(graph).dispatch("/goal clear") is True

    assert graph._interaction_mode.value == "auto"
    assert graph._task_run.goal == ""
    assert graph._task_state.pending_approval is None
