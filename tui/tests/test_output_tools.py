from tui_helpers import *  # noqa: F403

import sys

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.formatting import text_from_line


def test_turn_render_uses_full_width_user_background(tmp_path):
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("m_virtual_comment_views需要封装")

        lines = test_dock.tree.render(48)
        text = text_from_line(lines[0])

        assert text.plain.startswith("❯ m_virtual_comment_views需要封装")
        assert cell_len(text.plain) == 48
        assert any("on #3a3937" in str(span.style) for span in text.spans)
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_tool_call_renders_metadata_with_branch_rows():
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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
    from voidx.presentation.output.tree import OutputTree

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


def test_committed_todo_state_stays_internal_and_omits_progress_bar():
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
        node = test_dock.commit_todo_state()

        rendered = "\n".join(_rich_plain(line) for line in test_dock.tree.render(100))

        assert test_dock.todo_state() is None
        assert node is not None
        assert node.node_type == "todo"
        assert node.payload["summary"] == "1/2 done · 0 active · 1 pending"
        assert [item["content"] for item in node.payload["items"]] == [
            "finished task",
            "next task",
        ]
        assert "Todo:" not in rendered
        assert "finished task" not in rendered
        assert "next task" not in rendered
        assert "█" not in rendered
        assert "░" not in rendered
    finally:
        test_dock.deactivate()
        test_dock.reset()


