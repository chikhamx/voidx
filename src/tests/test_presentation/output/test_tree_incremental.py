

def test_remove_root_turn_subtree_keeps_indexes_and_render_equivalence():
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    first = tree.new_node(
        tree.root,
        node_type="turn",
        header="first",
        payload={"transcript_turn_id": 10, "durable": True, "committed": True},
    )
    tree.new_node(first, node_type="assistant", header="first answer")
    second = tree.new_node(
        tree.root,
        node_type="turn",
        header="second",
        payload={"transcript_turn_id": 11},
    )
    tree.new_node(second, node_type="assistant", header="second answer")
    third = tree.new_node(
        tree.root,
        node_type="turn",
        header="third",
        payload={"transcript_turn_id": 12},
    )
    tree.new_node(third, node_type="assistant", header="third answer")

    tree.render_with_click_map(80)
    removed = tree.remove_root_turn(10)

    expected = OutputTree()
    expected_second = expected.new_node(
        expected.root,
        node_type="turn",
        header="second",
        payload={"transcript_turn_id": 11},
    )
    expected.new_node(expected_second, node_type="assistant", header="second answer")
    expected_third = expected.new_node(
        expected.root,
        node_type="turn",
        header="third",
        payload={"transcript_turn_id": 12},
    )
    expected.new_node(expected_third, node_type="assistant", header="third answer")

    assert removed == (first,)
    assert [child.payload["transcript_turn_id"] for child in tree.root.children] == [11, 12]
    assert tree.get(first.id) is None
    assert tree.get(second.children[0].id) is second.children[0]
    assert tree.render(80) == expected.render(80)
    assert all(node_id in tree._all for node_id in tree._line_map.values())
    assert all(node_id in tree._all for node_id in tree._click_map.values())
    assert all(node_id in tree._all for node_id in tree._node_ranges)


def test_remove_root_turn_requires_settled_subtree():
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    turn = tree.new_node(
        tree.root,
        node_type="turn",
        header="pending",
        payload={"transcript_turn_id": 1},
    )
    child = tree.new_node(turn, node_type="assistant", header="pending answer")
    child.payload["render_pending"] = True

    assert tree.remove_root_turn(1) == ()
    assert tree.get(turn.id) is turn


def test_evictable_root_turns_require_durable_committed_and_unreferenced():
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    for turn_id in range(4):
        tree.new_node(
            tree.root,
            node_type="turn",
            header=f"turn {turn_id}",
            payload={
                "transcript_turn_id": turn_id,
                "durable": turn_id != 0,
                "committed": turn_id != 1,
                "referenced": turn_id == 2,
            },
        )

    assert tree.evictable_root_turn_ids() == [3]
    assert tree.evict_root_turns() == [3]
    assert [child.payload["transcript_turn_id"] for child in tree.root.children] == [0, 1, 2]
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



def test_remove_root_turn_removes_the_complete_logical_segment():
    safe = {
        "durable": True,
        "committed": True,
        "lifecycle": "completed",
        "active": False,
        "referenced": False,
        "pinned": False,
        "render_pending": False,
    }
    tree = OutputTree()
    turn = tree.new_node(
        tree.root,
        node_type="turn",
        header="user",
        payload={"transcript_turn_id": 7, **safe},
    )
    assistant = tree.new_node(
        tree.root,
        node_type="assistant",
        header="assistant",
        payload={**safe},
    )
    trailing = tree.new_node(
        tree.root,
        node_type="message",
        header="tool result",
        payload={**safe},
    )
    next_turn = tree.new_node(
        tree.root,
        node_type="turn",
        header="next",
        payload={"transcript_turn_id": 8, **safe},
    )

    assert tree.remove_root_turn(7) == (turn, assistant, trailing)
    assert tree.root.children == [next_turn]
    assert tree.get(assistant.id) is None
    assert tree.get(trailing.id) is None



def test_root_turn_eviction_rejects_missing_segment_safety_metadata():
    tree = OutputTree()
    turn = tree.new_node(
        tree.root,
        node_type="turn",
        header="user",
        payload={
            "transcript_turn_id": 1,
            "durable": True,
            "committed": True,
            "lifecycle": "completed",
            "active": False,
            "referenced": False,
            "pinned": False,
            "render_pending": False,
        },
    )
    tree.new_node(tree.root, node_type="assistant", header="missing metadata")

    assert tree.remove_root_turn(1) == ()
    assert tree.get(turn.id) is turn



def test_evict_root_turns_stops_at_the_first_unsafe_segment():
    safe = {
        "durable": True,
        "committed": True,
        "lifecycle": "completed",
        "active": False,
        "referenced": False,
        "pinned": False,
        "render_pending": False,
    }
    tree = OutputTree()
    for turn_id in (1, 2, 3):
        payload = {"transcript_turn_id": turn_id, **safe}
        if turn_id == 2:
            payload.pop("durable")
        tree.new_node(tree.root, node_type="turn", header=f"turn {turn_id}", payload=payload)

    assert tree.evict_root_turns() == [1]
    assert [
        node.payload.get("transcript_turn_id")
        for node in tree.root.children
        if node.node_type == "turn"
    ] == [2, 3]



def test_dock_turn_lifecycle_metadata_is_explicit():
    from voidx.presentation.output.dock import BottomInputDock

    presentation_dock = BottomInputDock()
    presentation_dock.begin_capture()
    turn = presentation_dock.start_turn("cancel me")

    assert turn.payload["lifecycle"] == "running"
    assert turn.payload["active"] is True
    assert turn.payload["transcript_turn_id"] == 0

    presentation_dock.end_turn(outcome="cancelled")

    assert turn.payload["lifecycle"] == "cancelled"
    assert turn.payload["active"] is False
    assert turn.payload["terminal"] is True
    assert presentation_dock.turn_in_progress is False


def test_dock_turn_terminal_metadata_covers_the_complete_segment():
    from voidx.presentation.output.dock import BottomInputDock

    presentation_dock = BottomInputDock()
    presentation_dock.begin_capture()
    turn = presentation_dock.start_turn("user")
    assistant = presentation_dock.ensure_agent()
    message = presentation_dock.append_message("answer")

    presentation_dock.end_turn(outcome="failed")

    segment = presentation_dock.tree.root_turn_segment(
        turn.payload["transcript_turn_id"]
    )
    assert assistant in segment
    assert message in segment
    for root in segment:
        stack = [root]
        while stack:
            node = stack.pop()
            assert node.payload["lifecycle"] == "failed"
            assert node.payload["terminal"] is True
            assert node.payload["active"] is False
            assert node.payload["committed"] is False
            assert node.payload["durable"] is False
            assert node.payload["referenced"] is False
            assert node.payload["pinned"] is False
            assert node.payload["render_pending"] is False
            stack.extend(node.children)


def test_mark_root_turns_committed_through_rendered_line_watermark():
    safe = {
        "durable": True,
        "committed": False,
        "lifecycle": "completed",
        "terminal": True,
        "active": False,
        "referenced": False,
        "pinned": False,
        "render_pending": False,
    }
    tree = OutputTree()
    first = tree.new_node(
        tree.root,
        node_type="turn",
        header="first",
        body_lines=["first body"],
        payload={"transcript_turn_id": 10, **safe},
    )
    first_child = tree.new_node(
        tree.root,
        node_type="assistant",
        header="first answer",
        payload={**safe},
    )
    second = tree.new_node(
        tree.root,
        node_type="turn",
        header="second",
        payload={"transcript_turn_id": 11, **safe},
    )
    tree.render(80)
    first_segment_lines = len(tree.render_root_slice(80, 0, 2))

    committed = tree.mark_root_turns_committed_through_line(
        80,
        first_segment_lines,
    )

    assert committed == [10]
    assert first.payload["committed"] is True
    assert first_child.payload["committed"] is True
    assert second.payload["committed"] is False
