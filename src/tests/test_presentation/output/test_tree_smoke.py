"""Smoke test for OutputNode / OutputTree rendering."""
import sys
import pytest
from rich.cells import cell_len
from rich.text import Text
from voidx.presentation.output.dock.app import BottomInputDock
from voidx.presentation.output.tree import OutputNode, OutputTree
from voidx.presentation.adapters.persistence.transcript_snapshot import transcript_rows_to_tree, tree_to_transcript_rows


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


def test_only_subagent_todo_nodes_render():
    tree = OutputTree()
    tree.new_node(
        tree.root,
        node_type="todo",
        header="[bold #A3BE8C]Todo[/]: [#8F9BA8]4/6 done · 1 active · 1 pending[/]",
    )
    tree.new_node(tree.root, node_type="turn", header="visible turn")
    hidden_assistant = tree.new_node(tree.root, node_type="assistant", header="")
    tree.new_node(
        hidden_assistant,
        node_type="todo",
        header="[bold #A3BE8C]Todo[/]: [#8F9BA8]1/1 done · 0 active · 0 pending[/]",
    )
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    subagent = tree.new_node(
        assistant,
        node_type="subagent",
        header="[#B48EAD]●[/#B48EAD] [bold]Mira[/bold](按设计)",
        agent_name="Mira",
    )
    tree.new_node(subagent, node_type="tool_call", header="[#EBCB8B]●[/#EBCB8B] Reading")
    tree.new_node(
        subagent,
        node_type="todo",
        header="[bold #A3BE8C]Todo[/]: [#8F9BA8]0/5 done · 1 active · 4 pending[/]",
    )

    lines = [_plain(line) for line in tree.render(120)]

    assert _plain(lines[0]).startswith("visible turn")
    assert lines[1] == ""
    assert any("Reading" in line for line in lines)
    assert sum("Todo:" in line for line in lines) == 1
    assert any("Todo: 0/5 done · 1 active · 4 pending" in line for line in lines)
    assert not any("Todo: 4/6 done" in line for line in lines)
    assert not any("Todo: 1/1 done" in line for line in lines)


def test_subagent_todo_keeps_connector_to_following_output():
    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    subagent = tree.new_node(
        assistant,
        node_type="subagent",
        header="● voidx",
        agent_name="voidx",
    )
    tree.new_node(subagent, node_type="status", header="Read schema")
    tree.new_node(
        subagent,
        node_type="todo",
        header="Todo: 2/5 done · 1 active · 2 pending",
        body_lines=["◐ implement schema"],
    )
    tree.new_node(subagent, node_type="assistant", header="Final report")

    lines = [_plain(line) for line in tree.render(120)]

    assert any("├─ Todo: 2/5 done · 1 active · 2 pending" in line for line in lines)
    assert any("│  ◐ implement schema" in line for line in lines)
    assert any("└─ Final report" in line for line in lines)


def test_regular_subagent_spaces_following_assistant_message():
    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    tree.new_node(
        assistant,
        node_type="subagent",
        header="● Reviewer(review changes) completed",
        agent_name="review",
        status="done",
    )
    tree.new_node(
        assistant,
        node_type="assistant",
        header="● 子代理返回异常，让我重新派发。",
    )

    lines = [_plain(line) for line in tree.render(120)]
    subagent_index = next(index for index, line in enumerate(lines) if "Reviewer(review changes)" in line)
    message_index = next(index for index, line in enumerate(lines) if "子代理返回异常" in line)

    assert lines[subagent_index + 1] == ""
    assert message_index == subagent_index + 2


def test_agent_tool_header_uses_raw_name():
    dock = BottomInputDock()
    assistant = dock.tree.new_node(dock.tree.root, node_type="assistant", header="● voidx")
    node = dock.start_tool(
        "Delegating",
        'name="voidx"',
        parent=assistant,
        tool_name="agent",
        raw_args={"name": "voidx"},
    )

    header = _plain(node.header)

    assert "voidx" in header
    assert "Reviewer" not in header
    assert 'Agent("voidx")' not in header


@pytest.mark.parametrize(
    ("raw_args", "expected"),
    [
        ({"op": "create", "paths": "src/new.py"}, 'Create("src/new.py")'),
        ({"op": "delete", "paths": "src/old.py"}, 'Remove("src/old.py")'),
        (
            {"op": "move", "moves": [{"src": "src/old.py", "dest": "src/new.py"}]},
            'Rename("old.py → new.py")',
        ),
        (
            {"op": "move", "moves": [{"src": "src/old.py", "dest": "lib/new.py"}]},
            'Move("src/old.py → lib/new.py")',
        ),
    ],
)
def test_manage_tool_header_uses_action_display(raw_args, expected):
    dock = BottomInputDock()
    assistant = dock.tree.new_node(dock.tree.root, node_type="assistant", header="● voidx")
    node = dock.start_tool(
        "Managing",
        'op="manage"',
        parent=assistant,
        tool_name="manage",
        raw_args=raw_args,
    )

    header = _plain(node.header)

    assert expected in header
    assert 'Manage("' not in header


def test_manage_tool_header_long_path_fits_one_visual_row():
    width = 72
    dock = BottomInputDock()
    assistant = dock.tree.new_node(dock.tree.root, node_type="assistant", header="● voidx")
    node = dock.start_tool(
        "Managing",
        'op="create"',
        parent=assistant,
        tool_name="manage",
        raw_args={"op": "create", "paths": "src/" + "很长的目录名/" * 12 + "created.py"},
    )

    lines = dock.tree.render(width)
    row = next(row for row, node_id in dock.tree._line_map.items() if node_id == node.id)
    line = lines[row]
    plain = Text.from_markup(line).plain

    assert "\n" not in plain
    assert cell_len(plain) <= width
    assert "Create(" in plain
    assert "…" in plain




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


def test_turn_with_ansi_prefix_closing_tag_does_not_crash():
    """Pasted segments wrapped with ANSI_LINE_PREFIX may contain rich markup
    closing tags like [/] in the markdown-rendered content.  _full_width_row
    must not raise MarkupError when computing visible length."""
    from voidx.presentation.output.dock.formatting import ANSI_LINE_PREFIX, text_from_line

    tree = OutputTree()
    # Simulate a pasted-segment body line: ANSI prefix + markdown line containing [/]
    body_line = ANSI_LINE_PREFIX + "code with [/] closing tag"
    tree.new_node(
        tree.root,
        node_type="turn",
        header=f"[bold white]❯[/] {ANSI_LINE_PREFIX}some [/] text",
        body_lines=[body_line],
    )

    # This used to raise MarkupError inside _full_width_row
    lines = tree.render(80)
    assert len(lines) > 0

    # The style wrapper's closing [/] must not leak into visible text.
    # User content [/] (inside the ANSI segment) is expected to remain as
    # literal text — only the wrapper tag should be consumed by markup parsing.
    for line in lines:
        plain = text_from_line(line).plain
        # The wrapper's [/] appears right before ANSI_LINE_PREFIX in the raw
        # line.  After text_from_line, it should be consumed as markup, so
        # the plain text should not contain a trailing [/] from the wrapper.
        # User-content [/] is inside the ANSI segment and stays as literal.
        assert not plain.rstrip().endswith("[/]"), (
            f"Wrapper closing tag leaked: {plain!r}"
        )



def test_render_tail_walks_only_the_visible_root_suffix(monkeypatch):
    tree = OutputTree()
    for index in range(1_000):
        tree.new_node(
            tree.root,
            node_type="message",
            header=f"history line {index}",
            collapsed=False,
        )

    tree.render(80)
    tree.new_node(
        tree.root,
        node_type="message",
        header="latest line",
        collapsed=False,
    )

    original_walk = tree._walk_render
    walked: list[str] = []

    def tracked_walk(node, *args, **kwargs):
        walked.append(node.id)
        return original_walk(node, *args, **kwargs)

    monkeypatch.setattr(tree, "_walk_render", tracked_walk)
    tail = tree.render_tail(80, 5)

    assert len(walked) <= 5
    assert tail == tree.render(80)[-5:]



def test_render_root_tail_excludes_prefix_without_full_render(monkeypatch):
    tree = OutputTree()
    for index in range(1_000):
        tree.new_node(
            tree.root,
            node_type="message",
            header=f"history line {index}",
            collapsed=False,
        )
    tree.new_node(
        tree.root,
        node_type="startup",
        header="startup banner",
        collapsed=False,
    )

    def fail_full_render(_width: int):
        raise AssertionError("root-tail rendering must not use full render")

    monkeypatch.setattr(tree, "render", fail_full_render)

    tail = tree.render_root_tail(80, 1, len(tree.root.children), 5)

    assert "startup banner" in "\n".join(tail)
    assert "history line 0" not in "\n".join(tail)
