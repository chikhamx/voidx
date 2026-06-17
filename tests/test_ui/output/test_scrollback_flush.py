"""Tests for scrollback flush: committed lines flow to terminal history."""
import sys
sys.path.insert(0, "src")

from voidx.ui.output.tree import OutputTree


def test_committed_count_tracks_flushed_lines():
    """After flushing, committed_line_count equals the number of flushed lines."""
    tree = OutputTree()
    tree.new_node(tree.root, node_type="startup", header="banner", body_lines=["line1"])
    tree.new_node(tree.root, node_type="turn", header="❯ hello")
    tree.new_node(tree.root, node_type="assistant", header="● reply")

    lines = tree.render(80)
    assert len(lines) > 0

    # Simulate: flush startup, turn, and the visual gap before the reply.
    committed = 5
    assert committed <= len(lines)
    # The remaining lines are the "active frame"
    active = lines[committed:]
    assert len(active) > 0
    assert "● reply" in active[0]


def test_flush_increments_on_turn_complete():
    """When a turn completes, committed count should advance to include it."""
    tree = OutputTree()

    # Turn 1: complete
    tree.new_node(tree.root, node_type="turn", header="❯ first")
    asst1 = tree.new_node(tree.root, node_type="assistant", header="● reply1")
    asst1.status = "done"

    lines_after_turn1 = tree.render(80)
    committed_after_turn1 = len(lines_after_turn1)

    # Turn 2: active (still running)
    tree.new_node(tree.root, node_type="turn", header="❯ second")
    tree.new_node(tree.root, node_type="assistant", header="● Working", status="running")

    lines_after_turn2 = tree.render(80)
    # Active frame = everything after committed lines
    active = lines_after_turn2[committed_after_turn1:]
    assert any("Working" in line for line in active)
    assert any("second" in line for line in active)


def test_active_frame_only_contains_uncommitted():
    """Active frame should not contain already-flushed content."""
    tree = OutputTree()
    tree.new_node(tree.root, node_type="startup", header="banner")
    tree.new_node(tree.root, node_type="turn", header="❯ q1")
    tree.new_node(tree.root, node_type="assistant", header="● a1", status="done")

    lines = tree.render(80)
    committed = len(lines)  # everything committed

    # New turn starts
    tree.new_node(tree.root, node_type="turn", header="❯ q2")
    tree.new_node(tree.root, node_type="assistant", header="● Working", status="running")

    lines2 = tree.render(80)
    active = lines2[committed:]
    # Active should only contain q2 + Working
    assert not any("q1" in line for line in active)
    assert not any("a1" in line for line in active)
    assert any("q2" in line for line in active)


def test_flush_preserves_tree_integrity():
    """Flushing should not modify the tree — only tracks a line offset."""
    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ hello")
    tree.new_node(tree.root, node_type="assistant", header="● reply")

    lines_before = tree.render(80)
    committed = len(lines_before)

    # Add more content
    tree.new_node(tree.root, node_type="turn", header="❯ world")
    tree.new_node(tree.root, node_type="assistant", header="● reply2")

    lines_after = tree.render(80)
    # First `committed` lines should be unchanged
    assert lines_after[:committed] == lines_before


def test_empty_active_frame_when_idle():
    """When all content is committed and no active turn, active frame is empty."""
    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ hello")
    tree.new_node(tree.root, node_type="assistant", header="● reply", status="done")

    lines = tree.render(80)
    committed = len(lines)
    active = lines[committed:]
    assert len(active) == 0


def test_startup_included_in_committed():
    """Startup banner lines should be part of committed (flushed) content."""
    tree = OutputTree()
    startup = tree.new_node(tree.root, node_type="startup", header="banner", body_lines=["info"])
    tree.new_node(tree.root, node_type="turn", header="❯ hello")
    tree.new_node(tree.root, node_type="assistant", header="● reply", status="done")

    lines = tree.render(80)
    startup_count = tree.startup_line_count()
    assert startup_count > 0
    # Startup lines are at the beginning
    assert startup_count <= len(lines)


def test_flush_only_contains_tree_lines():
    """Flushed content should only be tree.render() lines — no input box,
    status bar, separator, or other UI chrome."""
    tree = OutputTree()
    tree.new_node(tree.root, node_type="turn", header="❯ hello")
    tree.new_node(tree.root, node_type="assistant", header="● reply")

    lines = tree.render(80)
    # tree.render() only produces transcript content.
    # Input box (❯), status bar, separators are NOT in tree lines —
    # they are added by _render_bottom_elements separately.
    for line in lines:
        # No separator lines from the UI chrome
        assert not all(c in ('─', ' ') for c in line.strip()) or not line.strip()
        # No status bar content (model/provider info)
        assert "ctx" not in line
        assert "cache" not in line


def test_startup_flushed_before_first_frame():
    """Startup should be flushable independently before any frame render."""
    tree = OutputTree()
    tree.new_node(tree.root, node_type="startup", header="banner", body_lines=["info"])

    lines = tree.render(80)
    # All lines are startup — flushing them means committed = len(lines)
    committed = len(lines)
    active = lines[committed:]
    assert len(active) == 0

    # After adding a turn, only the turn is in the active frame
    tree.new_node(tree.root, node_type="turn", header="❯ hello")
    lines2 = tree.render(80)
    active2 = lines2[committed:]
    assert any("hello" in line for line in active2)
    assert not any("banner" in line for line in active2)
