from tui_helpers import *  # noqa: F403

import sys

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from voidx.presentation.output.dock import dock

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ user")
    tree.new_node(tree.root, node_type="assistant", header="● reply")

    lines = tree.render(80)

    assert "user" in _rich_plain(lines[0])
    assert lines[1] == ""
    assert "reply" in _rich_plain(lines[2])


def test_tree_inserts_gap_between_root_assistant_messages_without_spacer():
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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


def test_transient_retry_status_does_not_flush_before_uncommitted_stream():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.set_stream("streaming reply")
        test_dock.set_status(
            "llm:retry",
            "LLM error, retrying in 2s",
            "boom",
            stage="working",
        )

        lines = test_dock.tree.render(100)
        limit = test_dock.safe_flush_line_count(100, 0)
        flushed = "\n".join(_rich_plain(line) for line in lines[:limit])
        active = "\n".join(_rich_plain(line) for line in lines[limit:])

        assert "LLM error, retrying in 2s" not in flushed
        assert "LLM error, retrying in 2s" in active
        assert "boom" in active
        assert "streaming reply" in active
    finally:
        test_dock.deactivate()
        test_dock.reset()


