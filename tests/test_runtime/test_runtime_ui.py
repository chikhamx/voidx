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

        preloaded = [name for name in sys.modules if name.startswith("voidx.ui")]
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

        loaded = [name for name in sys.modules if name.startswith("voidx.ui")]
        if loaded:
            raise SystemExit(f"loaded UI modules: {loaded[:5]}")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
        },
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
