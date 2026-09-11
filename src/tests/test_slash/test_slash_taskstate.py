import pytest
from voidx.presentation.slash import SlashHandler
from tests.test_slash.context import command_context
from tests.test_slash.test_slash_image import FakeChoiceApp, RecordingUi


def setup_handler(app=None):
    ui = RecordingUi()
    state = {"strip": True}
    graph = command_context(app=app, ui=ui)
    graph.task_state_strip_enabled = lambda: state["strip"]
    graph.set_task_state_strip = lambda value: state.update(strip=value)
    return SlashHandler(graph), state, ui


@pytest.mark.asyncio
async def test_taskstate_command_switches_and_reports_status():
    handler, state, ui = setup_handler()
    assert await handler.dispatch('/taskstate status')
    assert any('taskstate strip: on' in line for line in ui.output)
    assert await handler.dispatch('/taskstate strip off')
    assert state['strip'] is False
    assert await handler.dispatch('/taskstate strip on')
    assert state['strip'] is True


@pytest.mark.asyncio
async def test_taskstate_picker_and_invalid_arguments():
    app = FakeChoiceApp(result='strip_off')
    handler, state, ui = setup_handler(app)
    assert await handler.dispatch('/taskstate')
    assert state['strip'] is False
    assert any('default' in str(c) for c in app.choices)
    await handler.dispatch('/taskstate strip nonsense')
    assert state['strip'] is False
    assert ui.errors
