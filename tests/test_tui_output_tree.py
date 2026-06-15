from tests.tui_helpers import *  # noqa: F403

import sys

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from voidx.ui.output.dock import dock

def test_capture_renderable_reuses_console_and_clears_buffer(tmp_path):
    tui = _tui(tmp_path)

    first = tui._capture_renderable(Text("first"), 80)
    console_id = id(tui._capture_console)
    second = tui._capture_renderable(Text("second"), 80)

    assert id(tui._capture_console) == console_id
    assert "first" in first
    assert "first" not in second
    assert "second" in second


def test_render_impl_reuses_base_bottom_row_count_when_unchanged(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    calls = 0
    original = tui._capture_renderable

    def counted_capture(renderable, width):
        nonlocal calls
        calls += 1
        return original(renderable, width)

    monkeypatch.setattr(tui, "_capture_renderable", counted_capture)

    tui._render_impl(height=24)
    assert calls == 1

    tui._render_impl(height=24)
    assert calls == 1


def test_render_impl_reuses_panel_capture_for_count_and_clipping(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    monkeypatch.setattr(
        tui,
        "_render_panel_lines",
        lambda width: [f"[bold]item {index}[/]" for index in range(8)],
    )
    calls = 0
    original = tui._capture_renderable

    def counted_capture(renderable, width):
        nonlocal calls
        calls += 1
        return original(renderable, width)

    monkeypatch.setattr(tui, "_capture_renderable", counted_capture)

    tui._render_impl(height=8)

    assert calls == 2


def test_input_history_is_bounded(tmp_path):
    tui = _tui(tmp_path)
    limit = tui.INPUT_HISTORY_LIMIT

    for index in range(limit + 5):
        tui._record_history(f"command {index}")

    assert len(tui._input_history) == limit
    assert tui._input_history[0] == "command 5"


def test_tree_incremental_render_only_rewalks_dirty_subtree():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    # Build a tree with two independent root children
    turn1 = tree.new_node(tree.root, node_type="turn", header="Turn 1")
    tree.new_node(turn1, node_type="message", header="msg1", body_lines=["body1"])
    turn2 = tree.new_node(tree.root, node_type="turn", header="Turn 2")
    stream = tree.new_node(turn2, node_type="assistant", header="stream", body_lines=["line 1"])

    # Full render
    full_render = tree.render(80)
    assert any("Turn 1" in line for line in full_render)
    assert any("Turn 2" in line for line in full_render)

    # Content-only update on the stream node
    stream.body_lines = ["line 1", "line 2 (new)"]
    tree.mark_dirty(stream.id)

    # Incremental render should still be correct
    # Verify incremental path was used (no full dirty flag)
    assert tree._dirty is False
    inc_render = tree.render(80)
    assert "line 2 (new)" in "\n".join(inc_render)
    assert any("Turn 1" in line for line in inc_render)
    assert any("Turn 2" in line for line in inc_render)
    assert len(inc_render) > len(full_render)  # grew by one line
    # Verify range: stream node's start position stays unchanged.
    assert tree._node_ranges[stream.id][0] >= 2  # after turn2 header

    # Third update – should still be correct
    stream.body_lines = ["line 1", "line 2 (new)", "line 3 (newest)"]
    tree.mark_dirty(stream.id)
    inc_render2 = tree.render(80)
    assert "line 3 (newest)" in "\n".join(inc_render2)
    # Verify no duplicate content from stale ranges
    joined = "\n".join(inc_render2)
    assert joined.count("line 1") == 1  # appears exactly once
    assert joined.count("line 3 (newest)") == 1

    # After structural change (add node), full render should work
    turn3 = tree.new_node(tree.root, node_type="turn", header="Turn 3")
    final = tree.render(80)
    assert any("Turn 1" in line for line in final)
    assert any("Turn 3" in line for line in final)


def test_tree_incremental_render_shifts_sibling_after_ranges():
    """After incremental splice, sibling-after nodes get shifted ranges."""
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="Turn")
    assistant = tree.new_node(tree.root, node_type="assistant", header="● Working")
    stream = tree.new_node(assistant, node_type="assistant", header="stream", body_lines=["line 1"])
    # sibling-after: same parent as stream
    tool = tree.new_node(assistant, node_type="tool_call", header="read file.py")

    full = tree.render(80)
    # Remember ranges after full render
    tool_start, tool_end = tree._node_ranges[tool.id]
    assistant_start, assistant_end = tree._node_ranges[assistant.id]

    # Content-only update on stream: it grows by 2 lines
    stream.body_lines = ["line 1", "extra line 2", "extra line 3"]
    tree.mark_dirty(stream.id)
    inc = tree.render(80)

    delta = len(inc) - len(full)
    assert delta == 2

    # tool's range should shift by delta
    new_tool_start, new_tool_end = tree._node_ranges[tool.id]
    assert new_tool_start == tool_start + delta
    assert new_tool_end == tool_end + delta

    # assistant's end should shift by delta (ancestor spanning)
    _, new_assistant_end = tree._node_ranges[assistant.id]
    assert new_assistant_end == assistant_end + delta

    # Content is still correct
    assert "extra line 3" in "\n".join(inc)
    assert "read file.py" in "\n".join(inc)


def test_output_tree_move_child_to_first_refreshes_sibling_flags():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    root = tree.root
    first = tree.new_node(root, node_type="message", header="first")
    second = tree.new_node(root, node_type="message", header="second")
    third = tree.new_node(root, node_type="message", header="third")

    tree.move_child_to_first(root, third)

    assert root.children == [third, first, second]
    assert third._is_last_sibling is False
    assert first._is_last_sibling is False
    assert second._is_last_sibling is True


def test_tree_incremental_render_shifts_click_map():
    """After incremental splice, _click_map rows shift correctly."""
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="Turn")
    assistant = tree.new_node(tree.root, node_type="assistant", header="Working")
    stream = tree.new_node(assistant, node_type="assistant", header="stream", body_lines=["L1"])
    tool = tree.new_node(assistant, node_type="tool_call", header="read file")

    full = tree.render(80)
    # tool is clickable
    tool_rows = [r for r, n in tree._click_map.items() if n == tool.id]
    assert len(tool_rows) == 1
    old_tool_row = tool_rows[0]
    assert "read file" in full[old_tool_row]

    # Content-only update: stream grows by 2 lines
    stream.body_lines = ["L1", "L2", "L3"]
    tree.mark_dirty(stream.id)
    inc = tree.render(80)

    # tool's click_map row should shift by delta=2
    new_tool_rows = [r for r, n in tree._click_map.items() if n == tool.id]
    assert len(new_tool_rows) == 1
    assert new_tool_rows[0] == old_tool_row + 2
    assert "read file" in inc[new_tool_rows[0]]

    # Second incremental: stream shrinks by 1 line
    stream.body_lines = ["L1", "L2"]
    tree.mark_dirty(stream.id)
    inc2 = tree.render(80)
    tool_rows2 = [r for r, n in tree._click_map.items() if n == tool.id]
    assert tool_rows2[0] == old_tool_row + 1
    assert "read file" in inc2[tool_rows2[0]]


def test_tree_line_map_tracks_non_clickable_body_rows():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    message = tree.new_node(
        tree.root,
        node_type="message",
        header="header",
        body_lines=["body"],
    )

    lines, line_map = tree.render_with_line_map(80)

    assert lines == ["header", "body"]
    assert line_map == {0: message.id, 1: message.id}
    assert tree._click_map == {}


def test_tree_inserts_gap_between_user_turn_and_assistant_without_spacer():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ user")
    tree.new_node(tree.root, node_type="assistant", header="● reply")

    lines = tree.render(80)

    assert "user" in _rich_plain(lines[0])
    assert lines[1] == ""
    assert "reply" in _rich_plain(lines[2])


def test_tree_inserts_gap_between_root_assistant_messages_without_spacer():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ user")
    tree.new_node(tree.root, node_type="assistant", header="● first reply")
    tree.new_node(tree.root, node_type="assistant", header="● second reply")

    lines = tree.render(80)

    first_index = next(index for index, line in enumerate(lines) if "first reply" in _rich_plain(line))
    second_index = next(index for index, line in enumerate(lines) if "second reply" in _rich_plain(line))
    assert lines[first_index + 1] == ""
    assert second_index == first_index + 2


def test_tree_inserts_gap_before_user_turn_after_assistant_without_spacer():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ first user")
    tree.new_node(tree.root, node_type="assistant", header="● first reply")
    tree.new_node(tree.root, node_type="turn", header="❯ next user")

    lines = tree.render(80)

    reply_index = next(index for index, line in enumerate(lines) if "first reply" in _rich_plain(line))
    next_user_index = next(index for index, line in enumerate(lines) if "next user" in _rich_plain(line))
    assert lines[reply_index + 1] == ""
    assert next_user_index == reply_index + 2


def test_agent_placeholder_does_not_render_as_visible_row():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.ensure_agent()

        rendered = "\n".join(_rich_plain(line) for line in test_dock.tree.render(80))

        assert "voidx" not in rendered
        assert "●" not in rendered
        assert not rendered.strip()
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_tool_without_prior_stream_renders_without_voidx_parent():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_tool(
            "Running",
            'command="pwd"',
            tool_name="bash",
            raw_args={"command": "pwd"},
        )

        plain_lines = [_rich_plain(line).strip() for line in test_dock.tree.render(100)]
        visible = [line for line in plain_lines if line]

        assert visible
        assert all("voidx" not in line for line in visible)
        assert visible[0].startswith("● Bash(")
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_finished_tool_under_transparent_agent_can_flush():
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

        lines = test_dock.tree.render(100)
        limit = test_dock.safe_flush_line_count(100, 0)

        assert limit == len(lines)
        assert "Read" in "\n".join(lines[:limit])
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_uncommitted_stream_under_transparent_agent_does_not_flush():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.set_stream("streaming reply")

        assert test_dock.safe_flush_line_count(100, 0) == 0
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_settled_root_log_flushes_before_uncommitted_stream():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.set_stream("streaming reply")
        test_dock.append_ansi("\x1b[2mLLM error, retrying in 2s: boom\x1b[0m")

        lines = test_dock.tree.render(100)
        limit = test_dock.safe_flush_line_count(100, 0)
        flushed = "\n".join(_rich_plain(line) for line in lines[:limit])
        active = "\n".join(_rich_plain(line) for line in lines[limit:])

        assert "LLM error, retrying in 2s: boom" in flushed
        assert "streaming reply" in active
    finally:
        test_dock.deactivate()
        test_dock.reset()


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
                {"content": "finished task", "status": "completed"},
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
