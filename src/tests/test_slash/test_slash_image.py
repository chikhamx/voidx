from __future__ import annotations

import pytest

from voidx.presentation.slash import SlashHandler
from tests.test_slash.context import command_context


class FakeChoiceApp:
    def __init__(self, result: str | None = None) -> None:
        self.result = result
        self.prompt: str = ""
        self.choices: list = []

    async def ask_choice(self, prompt: str, choices: list, **kwargs) -> str | None:
        self.prompt = prompt
        self.choices = choices
        return self.result


class RecordingUi:
    def __init__(self) -> None:
        self.output: list[str] = []
        self.errors: list[str] = []

    def print(self, text: str = "") -> None:
        self.output.append(str(text))

    def error(self, text: str) -> None:
        self.errors.append(str(text))


def _make_graph(app=None, ui=None):
    graph = command_context(
        app=app,
        ui=ui or RecordingUi(),
    )
    graph._image_strip = False
    graph.image_strip_enabled = property(lambda self: self._image_strip)
    graph.set_image_strip = lambda val: setattr(graph, "_image_strip", val)
    return graph


@pytest.mark.asyncio
async def test_image_slash_command_interactive_choice_strip_on() -> None:
    app = FakeChoiceApp(result="strip_on")
    ui = RecordingUi()
    graph = _make_graph(app=app, ui=ui)
    handler = SlashHandler(graph)

    handled = await handler.dispatch("/image")
    assert handled is True
    assert app.prompt == "Image handling"
    assert any("Strip on" in str(c) for c in app.choices)
    assert graph._image_strip is True
    assert any("image strip: on" in line for line in ui.output)


@pytest.mark.asyncio
async def test_image_slash_command_interactive_choice_strip_off() -> None:
    app = FakeChoiceApp(result="strip_off")
    ui = RecordingUi()
    graph = _make_graph(app=app, ui=ui)
    graph._image_strip = True
    handler = SlashHandler(graph)

    handled = await handler.dispatch("/image")
    assert handled is True
    assert graph._image_strip is False
    assert any("image strip: off" in line for line in ui.output)


@pytest.mark.asyncio
async def test_image_slash_command_direct_args() -> None:
    ui = RecordingUi()
    graph = _make_graph(ui=ui)
    handler = SlashHandler(graph)

    await handler.dispatch("/image strip on")
    assert graph._image_strip is True
    assert any("image strip: on" in line for line in ui.output)

    await handler.dispatch("/image strip off")
    assert graph._image_strip is False
    assert any("image strip: off" in line for line in ui.output)

    await handler.dispatch("/image on")
    assert graph._image_strip is True

    await handler.dispatch("/image off")
    assert graph._image_strip is False


@pytest.mark.asyncio
async def test_image_slash_command_no_app_shows_status() -> None:
    ui = RecordingUi()
    graph = _make_graph(app=None, ui=ui)
    handler = SlashHandler(graph)

    handled = await handler.dispatch("/image")
    assert handled is True
    assert any("image strip: off" in line for line in ui.output)
    assert any("Usage: /image" in line for line in ui.output)
