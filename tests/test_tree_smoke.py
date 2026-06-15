"""Smoke test for OutputNode / OutputTree rendering."""
import sys
sys.path.insert(0, "src")
import pytest
from rich.text import Text
from voidx.ui.output.dock.app import BottomInputDock
from voidx.ui.output.tree import OutputNode, OutputTree
from voidx.ui.transcript import transcript_rows_to_tree, tree_to_transcript_rows


def _plain(line: str) -> str:
    return Text.from_markup(line).plain


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


def test_collapsed_summary_hides_internal_node_ids():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="turn")
    tool = tree.new_node(turn, node_type="tool_call", header="read file.py", collapsed=True)
    result = tree.new_node(tool, node_type="tool_result", header="OK", collapsed=True)

    assert "[n" not in tool.collapse_summary
    assert "[n" not in result.collapse_summary
    assert "\\[" not in tool.collapse_summary
    assert "\\[" not in result.collapse_summary
    assert tree.get(tool.id) is tool


def test_expanded_view_hides_internal_node_ids():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="turn")
    tool = tree.new_node(turn, node_type="tool_call", header="read file.py", collapsed=True)

    expanded = "\n".join(tree.render_expanded(tool.id, 80))

    assert "[n" not in expanded
    assert "\\[" not in expanded


def test_agent_subagent_render_flattens_wrapper_node():
    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="● Working")
    agent_tool = tree.new_node(
        assistant,
        node_type="tool_call",
        header="● Reviewer",
        payload={"tool_name": "agent"},
    )
    subagent = tree.new_node(
        agent_tool,
        node_type="subagent",
        header="● review agent completed",
        agent_name="review",
    )
    tree.new_node(
        subagent,
        node_type="tool_call",
        header='● Map("src")',
    )
    tree.new_node(
        agent_tool,
        node_type="tool_result",
        header="review final",
    )

    lines = tree.render(100)
    rendered = "\n".join(lines)
    map_line = next(line for line in lines if 'Map("src")' in line)

    assert "review agent completed" not in rendered
    assert "Reviewer" in rendered
    assert 'Agent("review")' not in rendered
    assert "├" not in map_line.partition("Map")[0]
    assert "└" not in map_line.partition("Map")[0]
    assert "│" not in map_line.partition("Map")[0]


def test_transparent_subagent_spaces_ai_message_after_tools():
    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="● Working")
    agent_tool = tree.new_node(
        assistant,
        node_type="tool_call",
        header="● Reviewer",
        payload={"tool_name": "agent"},
    )
    subagent = tree.new_node(
        agent_tool,
        node_type="subagent",
        header="● review agent completed",
        agent_name="review",
    )
    tree.new_node(subagent, node_type="tool_call", header='● Read("core.py")')
    tree.new_node(subagent, node_type="tool_call", header='● Read("provider.py")')
    tree.new_node(subagent, node_type="assistant", header="● 审查报告引用的行号与当前代码不匹配。")
    tree.new_node(subagent, node_type="tool_call", header='● Search("while True")')

    lines = [_plain(line) for line in tree.render(120)]
    provider_index = next(index for index, line in enumerate(lines) if 'Read("provider.py")' in line)
    message_index = next(index for index, line in enumerate(lines) if "审查报告引用" in line)
    search_index = next(index for index, line in enumerate(lines) if 'Search("while True")' in line)

    assert lines[provider_index + 1] == ""
    assert message_index == provider_index + 2
    assert search_index == message_index + 1


@pytest.mark.parametrize(
    ("agent", "display"),
    [
        ("explore", "Explorer"),
        ("plan", "Planner"),
        ("implement", "Implementer"),
        ("review", "Reviewer"),
    ],
)
def test_agent_tool_header_uses_role_display_name(agent, display):
    dock = BottomInputDock()
    assistant = dock.tree.new_node(dock.tree.root, node_type="assistant", header="● voidx")
    node = dock.start_tool(
        "Delegating",
        f'agent="{agent}"',
        parent=assistant,
        tool_name="agent",
        raw_args={"agent": agent},
    )

    header = _plain(node.header)

    assert display in header
    assert f'Agent("{agent}")' not in header


def test_transcript_snapshot_round_trips_turn_tree():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="❯ inspect")
    assistant = tree.new_node(tree.root, node_type="assistant", header="● voidx")
    thought = tree.new_node(
        assistant,
        node_type="thought",
        header="Thinking",
        body_lines=["read files"],
        collapsed=True,
        meta="Thinking for 1s",
    )
    tool = tree.new_node(
        assistant,
        node_type="tool_call",
        header="Reading(file.py)",
        tool_call_id="call_1",
        payload={"tool_name": "read", "args": 'file_path="file.py"'},
    )
    tree.new_node(
        tool,
        node_type="tool_result",
        header="content",
        body_lines=["line 2"],
        tool_call_id="call_1",
        collapsed=False,
    )

    rows, turn_count = tree_to_transcript_rows("s1", tree)
    restored = transcript_rows_to_tree(rows)
    rendered = "\n".join(restored.render(100))

    assert turn_count == 1
    assert rows[0].turn_id == 0
    assert rows[0].node_id == 0
    assert rows[1].parent_node_id is None
    assert rows[2].parent_node_id == rows[1].node_id
    assert rows[3].tool_call_id == "call_1"
    assert rows[3].metadata["payload"]["tool_name"] == "read"
    assert thought.id != restored.root.children[1].children[0].id
    assert turn.header in rendered
    assert "Thinking" in rendered
    assert "content" in rendered


if __name__ == "__main__":
    test_basic_tree()
