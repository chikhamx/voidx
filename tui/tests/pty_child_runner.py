"""Child-process entry: real PureTui on a pty, driven by paste control keys.

Runs in the forked child; stdout/stdin are the pty slave. History and the
active subagent + Wait are built via control keys (after run() has set up the
tty), then resize/query/exit commands are served from bracketed-paste text.
"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.state import set_dock
from voidx.presentation.output.dock.app import BottomInputDock

set_dock(BottomInputDock())

from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.events.schema import (
    SubagentFinished, SubagentStarted, ToolFinished, ToolStarted,
)
from voidx_cli.app import PureTui

RESIZE_HEIGHTS = {"A": 20, "B": 16, "C": 11, "D": 24}
HISTORY = "H"
ACTIVE = "I"
FINISH = "F"
QUERY = "Q"
EXIT = "E"

tui = PureTui(SimpleNamespace(workspace="/tmp/pty-resize"), [])
triggered = []


def build_history():
    dock.begin_capture()
    for i in range(35):
        dock.tree.new_node(
            parent=dock.tree.root, node_type="message",
            header=f"PTY-HISTORY-{i:02d}", status="done",
        )
    tui._render_frame()
    tui._flush_committed(force=True)


def build_active():
    consumer = DockEventConsumer(dock)
    consumer.handle(SubagentStarted(
        agent_id=7, subagent_id="pty-agent", name="Prism",
        description="PTY-ACTIVE-AGENT",
    ))
    consumer.handle(ToolStarted(
        agent_id=7, tool_call_id="pty-read", label="Searching",
        tool_name="read", args='file_path="probe.py"',
        raw_args={"file_path": "probe.py"},
    ))
    consumer.handle(ToolStarted(
        agent_id=0, tool_call_id="pty-wait", label='Wait("Prism")',
        tool_name="wait", args='"Prism"', raw_args={"name": "Prism"},
    ))
    tui._busy = True
    tui._render_frame()




def finish_active():
    consumer = DockEventConsumer(dock)
    consumer.handle(ToolFinished(agent_id=0, tool_call_id="pty-wait", label='Wait("Prism")', elapsed=0.1))
    consumer.handle(SubagentFinished(agent_id=7, subagent_id="pty-agent", ok=True))
    tui._busy = False
    tui._render_frame()
    tui._flush_committed(force=True)

def handle_text(text):
    if text == HISTORY:
        build_history()
        return
    if text == ACTIVE:
        build_active()
        return
    if text == FINISH:
        finish_active()
        return
    height = RESIZE_HEIGHTS.get(text)
    if height is not None and height not in triggered:
        triggered.append(height)
        tui._resize_pending = True
    elif text == QUERY:
        sys.stdout.write(f"ROWS={tui._visible_committed_rows}\n")
        sys.stdout.flush()
    elif text == EXIT:
        tui._exit_app()
        return
    tui.invalidate()


_orig_process = tui._process_input


def _process_input(data):
    begin, end = b"\x1b[200~", b"\x1b[201~"
    if data.startswith(begin) and data.endswith(end):
        handle_text(data[len(begin):-len(end)].decode("utf-8", "replace"))
        return False
    return _orig_process(data)


tui._process_input = _process_input


async def _noop_submit(text):
    return True


async def _main():
    await tui.run(_noop_submit)


try:
    asyncio.run(_main())
except BaseException as exc:
    sys.stderr.write(f"CHILD-ERROR: {type(exc).__name__}: {exc}\n")
    sys.stderr.flush()
    raise
