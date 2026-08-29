"""Incremental OutputTree rendering contracts."""

from voidx.presentation.output.dock.app import BottomInputDock
from voidx.presentation.output.tree import OutputTree


def test_active_thinking_line_ids_uses_node_range_without_full_line_map(monkeypatch):
    dock = BottomInputDock()
    for index in range(1_000):
        dock.tree.new_node(
            dock.tree.root,
            node_type="message",
            header=f"history {index}",
        )
    stream = dock.tree.new_node(
        dock.tree.root,
        node_type="assistant",
        header="thinking",
        payload={"phase": "thinking"},
        body_lines=["partial"],
    )
    dock._stream_node = stream
    dock.tree.render_with_line_map(80)

    def fail_full_line_map(_width: int = 80):
        raise AssertionError("thinking stream must not scan the full line map")

    monkeypatch.setattr(dock.tree, "render_with_line_map", fail_full_line_map)

    rows = dock.active_thinking_stream_line_ids(80)

    assert rows == {
        row
        for row, node_id in dock.tree._line_map.items()
        if node_id == stream.id
    }


def test_root_slice_line_map_matches_full_render_for_trimmed_range():
    tree = OutputTree()
    for index in range(10):
        tree.new_node(
            tree.root,
            node_type="message",
            header=f"history {index}",
            body_lines=[f"body {index}"],
        )

    full_lines, full_map = tree.render_with_line_map(80)
    sliced_lines, sliced_map = tree.render_root_slice_with_line_map(80, 6, 10)

    start = full_lines.index("history 6")
    assert sliced_lines == full_lines[start:]
    assert sliced_map == {
        row - start: node_id
        for row, node_id in full_map.items()
        if row >= start
    }


def test_tail_subtree_splice_preserves_previous_frame_and_matches_full_render():
    tree = OutputTree()
    tree.new_node(tree.root, node_type="message", header="history")
    assistant = tree.new_node(tree.root, node_type="assistant", header="working")
    stream = tree.new_node(
        assistant,
        node_type="assistant",
        header="stream",
        body_lines=["one"],
    )
    tree.render_with_click_map(80)
    previous_lines = list(tree._cached_lines)
    cached_lines = tree._cached_lines
    line_map = tree._line_map
    click_map = tree._click_map

    walked: list[str] = []
    original_walk = tree._walk_render

    def tracked_walk(node, *args, **kwargs):
        walked.append(node.id)
        return original_walk(node, *args, **kwargs)

    tree._walk_render = tracked_walk
    stream.body_lines = ["one", "two", "three"]
    tree.mark_dirty(stream.id)
    incremental = tree.render(80)

    full = OutputTree()
    full.new_node(full.root, node_type="message", header="history")
    full_assistant = full.new_node(full.root, node_type="assistant", header="working")
    full.new_node(
        full_assistant,
        node_type="assistant",
        header="stream",
        body_lines=["one", "two", "three"],
    )
    full.render_with_click_map(80)

    assert cached_lines == previous_lines
    assert tree._cached_lines is not cached_lines
    assert line_map is tree._line_map
    assert click_map is tree._click_map
    assert walked == [stream.id]
    assert incremental == full._cached_lines
    assert tree._line_map == full._line_map
    assert tree._click_map == full._click_map
    assert tree._node_ranges == full._node_ranges


def test_incremental_collapse_removes_stale_descendant_ranges_and_maps():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="turn")
    tool = tree.new_node(tree.root, node_type="tool_call", header="tool")
    result = tree.new_node(
        tool,
        node_type="tool_result",
        header="result",
        body_lines=["detail"],
    )
    tree.render_with_click_map(80)
    assert result.id in tree._node_ranges

    tool.collapsed = True
    tree.mark_dirty(tool.id)
    incremental = tree.render(80)

    full = OutputTree()
    full.new_node(full.root, node_type="turn", header="turn")
    full_tool = full.new_node(full.root, node_type="tool_call", header="tool")
    full_tool.collapsed = True
    full.new_node(
        full_tool,
        node_type="tool_result",
        header="result",
        body_lines=["detail"],
    )
    full.render_with_click_map(80)

    assert incremental == full._cached_lines
    assert tree._line_map == full._line_map
    assert tree._click_map == full._click_map
    assert tree._node_ranges == full._node_ranges
    assert result.id not in tree._node_ranges
    assert result.id not in tree._node_prefixes
    assert all(node_id != result.id for node_id in tree._line_map.values())
