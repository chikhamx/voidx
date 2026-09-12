from voidx.presentation.output.console.app import VoidConsole


def test_void_console_debug_defaults_false():
    console = VoidConsole()

    assert console.debug is False


def test_void_console_debug_can_be_enabled_explicitly():
    console = VoidConsole()

    console.set_debug(True)

    assert console.debug is True
def test_void_console_tool_done_debug_mode_elapsed_format(monkeypatch):
    printed = []
    console = VoidConsole()
    console.set_debug(True)
    monkeypatch.setattr(console, "print", lambda msg: printed.append(msg))
    monkeypatch.setattr("voidx.presentation.output.console.app.via_events", lambda: False)
    from voidx.presentation.output.dock import dock
    dock.deactivate()

    console.tool_done("bash", 2.3, True)
    assert len(printed) == 1
    assert "2.3s" in printed[0]
    assert "(2.3s)" not in printed[0]

    printed.clear()
    console.tool_done("bash", 1.5, True)
    assert len(printed) == 1
    assert "1.5s" not in printed[0]
    assert "(1.5s)" not in printed[0]
