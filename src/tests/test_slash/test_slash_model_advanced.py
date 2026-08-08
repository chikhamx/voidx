import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from tests.test_slash.context import command_context
from voidx.agent.slash.runtime import _select_from_list
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.config import Config, McpServerConfig, PermissionMode, Settings
from voidx.platform.code_ide import CodeIde
from voidx.llm.domain.model import ModelConfig
from voidx.agent.domain.user_profile import UserProfile
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.llm.catalog import STATIC_MODELS
from voidx.llm.usage import UsageStats
from voidx.config.adapters.profile_repository import delete_model_profile_async
from voidx.presentation.tools.clipboard_image import ClipboardImageResult


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
async def test_language_and_tone_dispatch_updatesettings_and_live_config(tmp_path):
    settings = Settings(str(tmp_path))
    graph = command_context(
        config=Config(workspace=str(tmp_path)),
        settings=settings,
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
    graph = command_context(
        config=Config(workspace=str(tmp_path)),
        settings=settings,
        app=app,
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
    graph = command_context(
        config=Config(workspace=str(tmp_path)),
        settings=settings,
        app=app,
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
    graph = command_context(
        config=await settings.build_config(),
        settings=settings,
        app=app,
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
    graph = command_context(
        config=Config(workspace=str(tmp_path)),
        settings=settings,
        app=None,
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
    graph = command_context(
        config=await settings.build_config(),
        settings=settings,
        app=app,
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
    graph = command_context(
        config=await settings.build_config(),
        settings=settings,
        app=None,
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
    from voidx.presentation.commands import COMMANDS

    names = [name for name, _description in COMMANDS]

    assert "/lang" in names
    assert "/tone" in names
    assert "/lang auto" not in names
    assert "/tone default" not in names


def test_model_ctx_command_is_in_palette():
    from voidx.presentation.commands import COMMANDS

    assert ("/model ctx", "Set context window size") in COMMANDS

@pytest.mark.asyncio
async def test_permission_mode_without_args_uses_prompt_app_choice(tmp_path):
    settings = Settings(str(tmp_path))
    permission = PermissionService()
    app = FakeChoiceApp(result="project_trusted")
    graph = command_context(
        permission=permission,
        settings=settings,
        app=app,
    )

    assert await SlashHandler(graph).dispatch("/permission") is True

    assert app.prompt == "Permission mode"
    assert permission.permission_mode == "project_trusted"
    assert settings.get_permission_mode() == PermissionMode.PROJECT_TRUSTED


@pytest.mark.asyncio
async def test_mode_command_is_removed():
    graph = command_context(
        _interaction_mode=None,
        _plan_mode=False,
        app=None,
    )

    assert await SlashHandler(graph).dispatch("/mode goal") is False
    assert await SlashHandler(graph).dispatch("/mode") is False

    assert graph._interaction_mode is None
    assert graph._plan_mode is False


@pytest.mark.asyncio
async def test_code_ide_dispatch_saves_ghostty(tmp_path):
    settings = Settings(str(tmp_path))
    graph = command_context(settings=settings, app=None)

    assert await SlashHandler(graph).dispatch("/code-ide ghostty") is True

    assert settings.get_code_ide() == CodeIde.GHOSTTY


@pytest.mark.asyncio
async def test_code_ide_dispatch_rejects_gostty_typo(tmp_path):
    settings = Settings(str(tmp_path))
    graph = command_context(settings=settings, app=None)

    assert await SlashHandler(graph).dispatch("/code-ide gostty") is True

    assert settings.get_code_ide() == CodeIde.TRAE


@pytest.mark.asyncio
async def test_code_ide_dispatch_uses_choice_panel(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path))
    app = FakeChoiceApp(result=CodeIde.CURSOR.value)
    graph = command_context(settings=settings, app=app)

    monkeypatch.setattr("voidx.agent.slash.commands.ide.detect_code_ides", lambda: [])

    assert await SlashHandler(graph).dispatch("/code-ide") is True

    assert app.prompt == "Code IDE"
    assert any(choice[1] == CodeIde.GHOSTTY.value for choice in app.choices)
    assert settings.get_code_ide() == CodeIde.CURSOR


@pytest.mark.asyncio
async def test_plan_and_unplan_are_mode_aliases():
    graph = command_context(
        _interaction_mode=None,
        _plan_mode=False,
        app=None,
    )
    handler = SlashHandler(graph)

    assert await handler.dispatch("/plan") is True
    assert graph._interaction_mode.value == "plan"
    assert graph._plan_mode is True

    assert await handler.dispatch("/unplan") is True
    assert graph._interaction_mode.value == "auto"
    assert graph._plan_mode is False


@pytest.mark.asyncio
async def test_goal_dispatch_requires_goal_runtime_acceptance_condition():
    graph = command_context(
        _interaction_mode=None,
        _plan_mode=False,
        app=None,
        task_state=TaskState(),
    )

    assert await SlashHandler(graph).dispatch("/goal 优化 markdown 渲染截断") is True

    assert graph._interaction_mode is None
    assert graph._plan_mode is False
    assert graph.task_state.current_goal is None


@pytest.mark.asyncio
async def test_goal_clear_is_treated_as_missing_acceptance_condition():
    state = TaskState(
        current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
    )
    graph = command_context(
        _interaction_mode=None,
        _plan_mode=False,
        app=None,
        task_state=state,
    )

    assert await SlashHandler(graph).dispatch("/goal clear") is True

    assert graph._interaction_mode is None
    assert graph.task_state.current_goal is not None
    assert graph.task_state.current_goal.desc == "优化 markdown 渲染截断"
