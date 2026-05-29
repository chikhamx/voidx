import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler, _select_from_list
from voidx.config import Settings
from voidx.llm.catalog import STATIC_MODELS
from voidx.ui.app_parts.clipboard_image import ClipboardImageResult


class FakeChoiceApp:
    def __init__(self, result: str | None = None) -> None:
        self.result = result
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
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "default_profile": "mimo/mimo-v2.5",
            "profiles": {
                "mimo/mimo-v2.5": {"api_key": "sk-test-1"},
                "openai/gpt-4o": {"api_key": "sk-test-2"},
            },
        }),
        encoding="utf-8",
    )
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(
        config=settings.build_config(),
        _settings=settings,
        model=object(),
        _app=None,
    )

    await SlashHandler(graph)._model_list()


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
async def test_model_prompt_uses_prompt_app_text_input():
    app = FakeChoiceApp(result="sk-test")
    graph = SimpleNamespace(_app=app)

    result = await SlashHandler(graph)._prompt("API key", secret=True)

    assert result == "sk-test"
    assert app.text_prompt == "API key"
    assert app.text_secret is True


def test_model_config_provider_list_matches_catalog():
    from voidx.agent.slash import PROVIDERS

    assert set(STATIC_MODELS).issubset(PROVIDERS)


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
