from voidx.ui.output.console.app import VoidConsole


def test_void_console_debug_defaults_false():
    console = VoidConsole()

    assert console.debug is False


def test_void_console_debug_can_be_enabled_explicitly():
    console = VoidConsole()

    console.set_debug(True)

    assert console.debug is True
