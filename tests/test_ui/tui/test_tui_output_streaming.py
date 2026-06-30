from tui_helpers import *  # noqa: F403

import sys

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from voidx.ui.output.dock import dock


def test_stream_reply_aligns_with_user_turn_start():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("你好")
        test_dock.set_stream("好，我来看看。")

        raw_lines = test_dock.tree.render(100)
        plain_lines = [_rich_plain(line) for line in raw_lines]
        user_line = next(line for line in plain_lines if "你好" in line)
        reply_index = next(index for index, line in enumerate(plain_lines) if "好，我来看看。" in line)
        reply_line = raw_lines[reply_index]

        assert user_line.index("❯") == 0
        assert not reply_line.startswith(" ")
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_user_turn_and_stream_reply_are_separated_by_blank_line():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("review一下TUI的渲染那块")
        test_dock.set_stream("好的，我来审查 TUI 渲染模块。")

        plain_lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        user_index = next(index for index, line in enumerate(plain_lines) if "review一下" in line)
        reply_index = next(index for index, line in enumerate(plain_lines) if "好的，我来审查" in line)

        assert plain_lines[user_index + 1] == ""
        assert reply_index == user_index + 2
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_stream_reply_and_following_tool_share_same_ai_message_block():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("看看这个\n[attachments: docs/specs/code-review-2026-06-10.md]")
        test_dock.set_stream("我来验证这份审查报告中的问题是否与当前代码库一致。")
        test_dock.commit_stream()
        test_dock.start_tool(
            "Reading",
            'file_path="src/voidx/agent/graph/core.py"',
            tool_name="read",
            raw_args={"file_path": "src/voidx/agent/graph/core.py"},
        )

        plain_lines = [_rich_plain(line) for line in test_dock.tree.render(120)]
        reply_index = next(index for index, line in enumerate(plain_lines) if "我来验证" in line)
        read_index = next(index for index, line in enumerate(plain_lines) if "Read" in line)

        assert plain_lines[reply_index - 1] == ""
        assert read_index == reply_index + 1
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_thinking_stream_does_not_store_blank_placeholder_rows():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("看看现在的文件write工具")

        test_dock.set_stream("先看 write 工具", phase="thinking")
        first_lines = test_dock.tree.render(100)
        first_count = len(first_lines)
        stream_node = test_dock._stream_node
        assert stream_node is not None
        assert stream_node.header == ""
        assert stream_node.payload["phase"] == "thinking"
        assert len(stream_node.body_lines) == 1
        assert test_dock.safe_flush_line_count(100, 0) < first_count

        test_dock.set_stream(
            "先看 write 工具\n"
            "再看测试\n"
            "确认 append 行为",
            phase="thinking",
        )
        second_lines = test_dock.tree.render(100)

        assert len(second_lines) >= first_count
        assert test_dock.safe_flush_line_count(100, 0) < len(second_lines)
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_thinking_stream_wraps_long_content_to_visual_lines():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("trace thinking wrap")
        long_text = (
            "When thinking ends and text begins, the streaming node should render "
            "content by terminal visual rows instead of one long logical line."
        )

        test_dock.set_stream(long_text, phase="thinking")

        stream_node = test_dock._stream_node
        assert stream_node is not None
        assert len(stream_node.body_lines) > 1
        assert len(stream_node.body_lines) <= 5
        for line in stream_node.body_lines:
            text = Text.from_ansi(line.removeprefix("\x00voidx-ansi\x00"))
            assert cell_len(text.plain) <= test_dock._markdown_width() + 2
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_turn_render_uses_full_width_user_background(tmp_path):
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("m_virtual_comment_views需要封装")

        lines = test_dock.tree.render(48)
        text = Text.from_markup(lines[0])

        assert text.plain.startswith("❯ m_virtual_comment_views需要封装")
        assert cell_len(text.plain) == 48
        assert any("on #3a3937" in str(span.style) for span in text.spans)
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_tool_call_renders_metadata_with_branch_rows():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="● voidx")
    tool = tree.new_node(
        assistant,
        node_type="tool_call",
        header="[#A3BE8C]●[/#A3BE8C] [bold]Bash[/bold](rg error)",
        body_lines=[
            "[dim]Running in the background (↓ to manage)[/dim]",
            "[dim](timeout 2m)[/dim]",
        ],
    )

    lines = tree.render(80)
    plain_lines = [_rich_plain(line).lstrip() for line in lines]

    assert plain_lines[1].startswith("● Bash(rg error)")
    assert plain_lines[2].startswith("└ Running in the background")
    assert plain_lines[3].startswith("└ (timeout 2m)")
    assert all("├" not in line.partition("Bash")[0] for line in plain_lines if "Bash" in line)
    assert tree._line_map[1] == tool.id
    assert tree._line_map[2] == tool.id


def test_tool_call_text_aligns_with_assistant_text_start():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="● voidx")
    tree.new_node(
        assistant,
        node_type="assistant",
        header="[#A3BE8C]●[/#A3BE8C] reply text",
    )
    tree.new_node(
        assistant,
        node_type="tool_call",
        header="[#A3BE8C]●[/#A3BE8C] [bold]Read[/bold](\"src/file.py\")",
        body_lines=["[dim]loading[/dim]"],
    )

    plain_lines = [_rich_plain(line) for line in tree.render(80)]
    reply_line = next(line for line in plain_lines if "reply text" in line)
    read_line = next(line for line in plain_lines if "Read" in line)
    metadata_line = next(line for line in plain_lines if "loading" in line)

    assert read_line.index("Read") == reply_line.index("reply text")
    assert metadata_line.index("loading") == reply_line.index("reply text")


def test_agent_text_blocks_are_spaced_after_tool_calls():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    tree.new_node(assistant, node_type="assistant", header="开始修复。")
    tree.new_node(assistant, node_type="tool_call", header="[#A3BE8C]●[/#A3BE8C] [bold]Bash[/bold](sed)")
    tree.new_node(assistant, node_type="tool_call", header="[#A3BE8C]●[/#A3BE8C] [bold]Bash[/bold](rg)")
    tree.new_node(assistant, node_type="assistant", header="现在确认一下。")
    tree.new_node(assistant, node_type="assistant", header="继续说明。")

    plain_lines = [_rich_plain(line) for line in tree.render(80)]

    first_text = plain_lines.index("开始修复。")
    first_tool = next(index for index, line in enumerate(plain_lines) if "Bash(sed)" in line)
    second_tool = next(index for index, line in enumerate(plain_lines) if "Bash(rg)" in line)
    second_text = plain_lines.index("现在确认一下。")
    third_text = plain_lines.index("继续说明。")

    assert first_tool == first_text + 1
    assert second_tool == first_tool + 1
    assert plain_lines[second_tool + 1] == ""
    assert second_text == second_tool + 2
    assert plain_lines[second_text + 1] == ""
    assert third_text == second_text + 2


def test_thinking_stream_starts_immediately_after_last_tool_call_without_header():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    tree.new_node(
        assistant,
        node_type="tool_call",
        header='[#A3BE8C]●[/#A3BE8C] [bold]Read[/bold]("src/a.py")',
    )
    tree.new_node(
        assistant,
        node_type="tool_call",
        header='[#A3BE8C]●[/#A3BE8C] [bold]Bash[/bold]("git diff")',
    )
    tree.new_node(
        assistant,
        node_type="assistant",
        header="",
        body_lines=["\x00voidx-ansi\x00  Let me check the diff."],
        payload={"phase": "thinking"},
    )

    plain_lines = [_rich_plain(line) for line in tree.render(100)]
    bash_index = next(index for index, line in enumerate(plain_lines) if "Bash" in line)
    thinking_index = next(index for index, line in enumerate(plain_lines) if "Let me check" in line)

    assert thinking_index == bash_index + 1
    assert all("Thinking" not in line for line in plain_lines)


def test_text_stream_after_thinking_restores_gap_after_tool_call():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("demo")
        test_dock.start_tool(
            "Bash",
            'command="pytest"',
            tool_name="bash",
            raw_args={"command": "pytest"},
        )
        test_dock.finish_tool("Bash", 0.1, True)
        test_dock.set_stream("checking result", phase="thinking")
        test_dock.tree.render(100)

        test_dock.set_stream("final answer", phase="text")

        plain_lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        bash_index = next(index for index, line in enumerate(plain_lines) if "Bash" in line)
        answer_index = next(index for index, line in enumerate(plain_lines) if "final answer" in line)

        assert plain_lines[bash_index + 1] == ""
        assert answer_index == bash_index + 2
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_file_change_body_lines_do_not_render_as_tool_metadata():
    test_dock = dock
    test_dock.begin_capture()
    try:
        tool = test_dock.start_tool(
            "Editing",
            'file_path="src/app.py"',
            tool_name="edit",
            raw_args={"file_path": "src/app.py"},
        )
        test_dock.append_file_change(
            "\n".join(
                [
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    "@@ -1,3 +1,3 @@",
                    " alpha",
                    "-old",
                    "+new",
                    " omega",
                ]
            ),
            parent=tool,
        )

        plain_lines = [_rich_plain(line).lstrip() for line in test_dock.tree.render(100)]
        diff_lines = [
            line
            for line in plain_lines
            if "old" in line or "new" in line or "alpha" in line or "omega" in line
        ]

        assert diff_lines
        assert all(not line.startswith("└") for line in diff_lines)
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_file_change_add_remove_background_extends_to_render_width():
    test_dock = dock
    test_dock.begin_capture()
    try:
        tool = test_dock.start_tool(
            "Editing",
            'file_path="src/app.py"',
            tool_name="edit",
            raw_args={"file_path": "src/app.py"},
        )
        test_dock.append_file_change(
            "\n".join(
                [
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    "@@ -1,2 +1,2 @@",
                    "-old",
                    "+new",
                ]
            ),
            parent=tool,
        )

        lines = test_dock.tree.render(72)
        changed = [
            Text.from_markup(line)
            for line in lines
            if "old" in Text.from_markup(line).plain or "new" in Text.from_markup(line).plain
        ]

        assert len(changed) == 2
        for text in changed:
            assert cell_len(text.plain) == 72
            assert text.plain.endswith(" ")
            assert any("on #003b0a" in str(span.style) or "on #4a0000" in str(span.style) for span in text.spans)
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_committed_todo_state_omits_progress_bar():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.set_todo_state(
            "1/2 done · 0 active · 1 pending",
            [
                {"content": "finished task", "status": "done"},
                {"content": "next task", "status": "pending"},
            ],
        )
        test_dock.commit_todo_state()

        rendered = "\n".join(_rich_plain(line) for line in test_dock.tree.render(100))

        assert "Todo: 1/2 done" in rendered
        assert "finished task" in rendered
        assert "next task" in rendered
        assert "█" not in rendered
        assert "░" not in rendered
    finally:
        test_dock.deactivate()
        test_dock.reset()


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

    from voidx.ui.tui import _dump_transcript_log

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


def test_start_turn_pasted_segment_has_ansi_prefix():
    """Pasted segment body_lines must carry ANSI_LINE_PREFIX for dock rendering."""
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
        has_ansi = any(
            line.startswith("\x00voidx-ansi\x00")
            for line in all_lines
        )
        assert has_ansi
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
