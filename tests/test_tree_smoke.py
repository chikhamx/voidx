"""Smoke test for OutputNode / OutputTree rendering."""
import sys
sys.path.insert(0, "src")
from voidx.ui.tree import OutputNode, OutputTree


def test_basic_tree():
    tree = OutputTree()

    # Turn 1
    turn1 = tree.new_node(
        tree.root,
        node_type="turn",
        header="Turn 1: analysis",
        status="done",
        elapsed=2.3,
    )
    tc1 = tree.new_node(
        turn1,
        node_type="tool_call",
        header="read file.py",
        status="done",
        elapsed=0.5,
    )
    tree.new_node(
        tc1,
        node_type="tool_result",
        header="OK",
        body_lines=["line 1: import os", "line 2: import sys"],
        status="done",
        elapsed=0.1,
    )
    tc2 = tree.new_node(
        turn1,
        node_type="tool_call",
        header="grep pattern",
        status="done",
        elapsed=1.2,
    )
    tree.new_node(
        tc2,
        node_type="tool_result",
        header="3 matches",
        body_lines=["file.py:10: pattern"],
        status="done",
        elapsed=0.05,
    )

    # Turn 2
    turn2 = tree.new_node(
        tree.root,
        node_type="turn",
        header="Turn 2: summary",
        status="done",
        elapsed=0.8,
    )
    tree.new_node(
        turn2,
        node_type="message",
        header="Final answer",
        body_lines=["Here is the result."],
        status="done",
    )

    # Collapse first tool call
    tc1.collapsed = True

    print("=== FULL RENDER (tc1 collapsed) ===")
    for i, line in enumerate(tree.render(80)):
        print(f"{i:3d} {line!r}")

    print(f"\nClick map: {tree._click_map}")

    print("\n=== collapse_summary for tc1 ===")
    print(tc1.collapse_summary)

    print("\n=== render_expanded(tc1) ===")
    for line in tree.render_expanded("n2", 80):
        print(repr(line))

    print("\n=== expand_all ===")
    tree.expand_all()
    for line in tree.render(80):
        print(line)

    print("\n=== collapse_all(max_depth=2) ===")
    tree.collapse_all(max_depth=2)
    for line in tree.render(80):
        print(line)

    print("\n=== _is_last_sibling checks ===")
    print(f"turn1._is_last_sibling: {turn1._is_last_sibling}")  # False (turn2 is last)
    print(f"turn2._is_last_sibling: {turn2._is_last_sibling}")  # True
    print(f"tc1._is_last_sibling: {tc1._is_last_sibling}")  # False (tc2 is last)
    print(f"tc2._is_last_sibling: {tc2._is_last_sibling}")  # True


if __name__ == "__main__":
    test_basic_tree()
