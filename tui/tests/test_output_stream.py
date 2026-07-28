from tui_helpers import *  # noqa: F403

import sys

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from voidx.ui.output.dock import dock
from voidx.ui.output.dock.formatting import text_from_line
from voidx.ui.output.console import StreamingRenderer


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


def test_streaming_renderer_commits_thinking_only_output():
    test_dock = dock
    test_dock.begin_capture()
    try:
        renderer = StreamingRenderer(Console(), stream_to_dock=True)
        renderer.feed_thinking("checking tool permissions")
        renderer.done()

        rendered = "\n".join(_rich_plain(line) for line in test_dock.tree.render(100))
        assert "checking tool permissions" not in rendered
        thinking_nodes = [
            node
            for parent in test_dock.tree.root.children
            for node in [parent, *parent.children]
            if node.node_type == "assistant" and node.payload.get("phase") == "thinking"
        ]
        assert thinking_nodes == []
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_thinking_only_stream_not_flushed_to_scrollback():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("问题")
        long_thinking = "\n".join(f"thinking line {i}" for i in range(10))
        test_dock.set_stream(long_thinking, phase="thinking")
        test_dock.commit_stream()

        flush_limit = test_dock.safe_flush_line_count(100, 0)
        lines = test_dock.tree.render(100)
        flushable = "\n".join(_rich_plain(line) for line in lines[:flush_limit])

        assert "thinking line" not in flushable
    finally:
        test_dock.deactivate()
        test_dock.reset()
