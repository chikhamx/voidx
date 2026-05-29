import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich.cells import cell_len

from voidx.ui.console import VoidConsole
from voidx.ui.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.startup import show_startup


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line.replace(ANSI_LINE_PREFIX, "")).rstrip()


def test_startup_banner_has_no_internal_blank_rows():
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        show_startup(
            VoidConsole(),
            model="mimo-v2.5",
            provider="mimo",
            workspace="/Users/chikham/workspace/voidx",
            session_title="你好",
            is_new=False,
        )

        lines = [_plain(line) for line in test_dock.tree.render(80)]

        assert lines
        assert all(line.strip() for line in lines)
        assert len(lines) <= 8
        assert max(cell_len(line) for line in lines) <= 80

        text = "\n".join(lines)
        assert "/\\________/\\    ╭╮" in text
        assert "◒      ◒" in text
        assert "╭╮" in text
        assert "o     O" in text
        assert "Workspace" in text
        assert "Ask anything" in text
        assert "/model switch" in text
        assert "•_•" not in text
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)
