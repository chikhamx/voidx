from tui_helpers import *  # noqa: F403

import sys

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.formatting import text_from_line


def test_safe_flush_line_count_stops_at_unsettled_stream_after_finished_tool():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("demo")
        tool = test_dock.start_tool(
            "Reading",
            'file_path="x.py"',
            tool_name="read",
            raw_args={"file_path": "x.py"},
        )
        test_dock.finish_tool_node(tool, "Read", 0.1, True)
        test_dock.append_tool_result("result")
        test_dock.set_stream("● final answer")

        lines = test_dock.tree.render(100)
        limit = test_dock.safe_flush_line_count(100, 0)

        assert 0 < limit < len(lines)
        assert "Read" in "\n".join(lines[:limit])
        assert "final answer" in "\n".join(lines[limit:])
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_safe_flush_line_count_requires_settled_ancestors():
    test_dock = dock
    test_dock.begin_capture()
    parent = test_dock.tree.new_node(
        test_dock.tree.root,
        node_type="subagent",
        header="explore agent",
        collapsed=False,
    )
    child = test_dock.tree.new_node(
        parent,
        node_type="assistant",
        header="found auth flow",
        collapsed=False,
    )
    test_dock.mark_node_unsettled(parent)
    test_dock.mark_node_settled(child)

    assert test_dock.safe_flush_line_count(100, 0) == 0

    test_dock.mark_node_settled(parent)
    assert test_dock.safe_flush_line_count(100, 0) == len(test_dock.tree.render(100))


def test_non_tty_flush_committed_prints_settled_prefix_before_idle(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    test_dock = dock
    test_dock.begin_capture()
    test_dock.start_turn("hello")

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})

    tui._flush_committed()

    assert "hello" in fake_stdout.text
    assert tui._committed_line_count > 0


def test_dump_transcript_log_writes_plain_text(tmp_path):
    tui = _tui(tmp_path)

    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="turn",
        header="[bold white]❯[/] hello world",
        body_lines=["this is a test message"],
        collapsed=False,
    )
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="[#EBCB8B]●[/#EBCB8B] response",
        body_lines=["some output here"],
        collapsed=False,
    )

    from voidx_cli import _dump_transcript_log

    log_path = tmp_path / ".voidx" / "transcript.log"
    assert not log_path.exists()

    _dump_transcript_log(tmp_path, dock.tree)

    assert log_path.exists()
    content = log_path.read_text()
    assert "hello world" in content
    assert "this is a test message" in content
    assert "some output here" in content


def test_start_turn_renders_pasted_block_without_tag_text():
    """Pasted blocks should be Markdown-rendered, <pasted> tags must not appear."""
    test_dock = dock
    test_dock.begin_capture()
    try:
        text = "fix this bug\n<pasted>\ndef foo():\n    pass\n</pasted>\nplease help"
        test_dock.start_turn(text)

        plain_lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        joined = "\n".join(plain_lines)

        assert "<pasted>" not in joined
        assert "</pasted>" not in joined
        assert "def foo():" in joined
        assert "fix this bug" in joined
        assert "please help" in joined
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_start_turn_pasted_content_stripped_of_tags():
    """Pasted content is rendered as plain text without <pasted> wrapper tags.

    No ANSI_LINE_PREFIX markers — content is escape()'d plain text.
    """
    test_dock = dock
    test_dock.begin_capture()
    try:
        text = (
            "<pasted>\n"
            "Here is some code:\n\n"
            "```python\n"
            "def foo():\n"
            "    pass\n"
            "```\n"
            "</pasted>"
        )
        test_dock.start_turn(text)

        turn_node = test_dock._current_turn
        assert turn_node is not None
        all_lines = [turn_node.header, *turn_node.body_lines]
        joined = "\n".join(all_lines)

        assert "<pasted>" not in joined
        assert "</pasted>" not in joined
        assert "def foo():" in joined
        assert "pass" in joined
        # No ANSI markers — plain text rendering
        assert not any(
            line.startswith("\x00voidx-ansi\x00")
            for line in all_lines
        )
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_start_turn_plain_text_unchanged_without_pasted_tags():
    """Messages without <pasted> tags should behave exactly as before."""
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("hello world")

        turn_node = test_dock._current_turn
        assert turn_node is not None
        assert "hello world" in _rich_plain(turn_node.header)
        assert turn_node.body_lines == []
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_start_turn_empty_pasted_block_at_start_has_nonempty_header():
    """Empty pasted block at start should not leave header as trailing space."""
    test_dock = dock
    test_dock.begin_capture()
    try:
        text = "<pasted>\n\n</pasted>\nreal content here"
        test_dock.start_turn(text)

        turn_node = test_dock._current_turn
        assert turn_node is not None
        header_plain = _rich_plain(turn_node.header)
        assert header_plain.strip() != ""
        assert "real content here" in header_plain or any(
            "real content here" in _rich_plain(line) for line in turn_node.body_lines
        )
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_start_turn_pasted_content_rendered_as_part_of_user_message():
    """Pasted content is rendered as part of the user message as plain text,
    without <pasted> wrapper tags or separate segment handling."""
    test_dock = dock
    test_dock.begin_capture()
    try:
        text = "fix this bug\n<pasted>\ndef foo():\n    pass\n</pasted>\nplease help"
        test_dock.start_turn(text)

        turn_node = test_dock._current_turn
        assert turn_node is not None
        all_lines = [turn_node.header, *turn_node.body_lines]
        joined_raw = "\n".join(all_lines)

        # <pasted> tags must not appear
        assert "<pasted>" not in joined_raw
        assert "</pasted>" not in joined_raw
        # Content from both pasted and non-pasted portions is present
        assert "fix this bug" in joined_raw
        assert "def foo():" in joined_raw
        assert "please help" in joined_raw
    finally:
        test_dock.deactivate()
        test_dock.reset()
