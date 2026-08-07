import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_noop_ui_sink_does_not_load_ui_modules():
    code = textwrap.dedent(
        """
        import sys

        from voidx.runtime.ui import console, ui, use_noop_ui_sink

        preloaded = [name for name in sys.modules if name.startswith("voidx.presentation")]
        if preloaded:
            raise SystemExit(f"preloaded UI modules: {preloaded[:5]}")

        use_noop_ui_sink()
        ui.set_debug(True)
        ui.print("hello")
        ui.warn("warn")
        ui.error("error")
        ui.step_header("agent")
        ui.tool_call("read", {})
        ui.tool_done("read", 0.1)
        ui.tool_result("ok")
        ui.diff("--- a/file")
        console.print("console")
        with console:
            console.print("captured")

        loaded = [name for name in sys.modules if name.startswith("voidx.presentation")]
        if loaded:
            raise SystemExit(f"loaded UI modules: {loaded[:5]}")
        """
    )

    src_dir = str(Path(__file__).parents[2])
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": src_dir,
        },
        cwd=src_dir,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_console_proxy_supports_rich_live_context_manager():
    from rich.console import Console
    from rich.live import Live

    from voidx.runtime.ui import console, reset_ui_sink, set_ui_sink

    class Sink:
        width = 80

        def __init__(self) -> None:
            self.console = Console(record=True, force_terminal=False)

    set_ui_sink(Sink())
    try:
        live = Live("hello", console=console, auto_refresh=False, transient=False)
        live.start()
        live.stop()
    finally:
        reset_ui_sink()


def test_frontend_factory_registers_and_creates_frontend():
    import voidx.runtime.ui as runtime_ui

    created = []

    class FakeFrontend:
        def __init__(self, status, commands):
            self.status = status
            self.commands = commands

    original_factory = runtime_ui._default_frontend_factory
    runtime_ui.reset_default_frontend()
    try:
        runtime_ui.register_default_frontend(lambda status, commands: FakeFrontend(status, commands))

        frontend = runtime_ui.create_frontend("status", [("/help", "Help")])

        assert isinstance(frontend, FakeFrontend)
        assert frontend.status == "status"
        assert frontend.commands == [("/help", "Help")]
        assert created == []
    finally:
        runtime_ui.register_default_frontend(original_factory)


def test_frontend_factory_errors_without_registered_frontend():
    import voidx.runtime.ui as runtime_ui

    original_factory = runtime_ui._default_frontend_factory
    runtime_ui.reset_default_frontend()
    try:
        try:
            runtime_ui.create_frontend("status", [])
        except RuntimeError as exc:
            assert "No frontend registered" in str(exc)
        else:
            raise AssertionError("create_frontend should fail without a registered frontend")
    finally:
        runtime_ui.register_default_frontend(original_factory)
