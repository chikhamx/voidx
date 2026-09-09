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


def test_ai_and_tool_blocks_are_compact_while_other_blocks_are_spaced():
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    tree.new_node(assistant, node_type="assistant", header="开始修复。")
    tree.new_node(assistant, node_type="tool_call", header="[#A3BE8C]●[/#A3BE8C] [bold]Bash[/bold](sed)")
    tree.new_node(assistant, node_type="tool_call", header="[#A3BE8C]●[/#A3BE8C] [bold]Bash[/bold](rg)")
    tree.new_node(assistant, node_type="assistant", header="现在确认一下。")
    tree.new_node(assistant, node_type="assistant", header="继续说明。")
    tree.new_node(assistant, node_type="clarify", header="● voidx clarify answered")
    tree.new_node(assistant, node_type="checkpoint", header="● voidx plan approved")

    plain_lines = [_rich_plain(line) for line in tree.render(80)]

    first_text = plain_lines.index("开始修复。")
    first_tool = next(index for index, line in enumerate(plain_lines) if "Bash(sed)" in line)
    second_tool = next(index for index, line in enumerate(plain_lines) if "Bash(rg)" in line)
    second_text = plain_lines.index("现在确认一下。")
    third_text = plain_lines.index("继续说明。")
    clarify = next(
        index for index, line in enumerate(plain_lines) if "voidx clarify answered" in line
    )
    checkpoint = next(
        index for index, line in enumerate(plain_lines) if "voidx plan approved" in line
    )

    assert first_tool == first_text + 1
    assert second_tool == first_tool + 1
    assert second_text == second_tool + 2
    assert plain_lines[second_text - 1] == ""
    assert third_text == second_text + 2
    assert plain_lines[third_text - 1] == ""
    assert plain_lines[third_text + 1] == ""
    assert clarify == third_text + 2
    assert plain_lines[clarify + 1] == ""
    assert checkpoint == clarify + 2




def test_tool_result_is_a_completed_sibling_and_keeps_request_unchanged():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("read the file")
        tool = test_dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-1",
            raw_args={"file_path": "src/app.py"},
        )
        request_header = tool.header
        result = test_dock.append_tool_result(
            "file contents",
            parent=tool,
            tool_call_id="read-1",
        )

        assert result is not None
        assert result.node_type == "tool_result"
        assert result.parent is tool.parent
        assert result in tool.parent.children
        assert tool.header == request_header
        assert tool.body_lines == []
        assert result.payload["lifecycle"] == "completed"
        assert result.id not in test_dock._settled_node_ids
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_file_change_result_does_not_rewrite_tool_request():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("update the file")
        tool = test_dock.start_tool(
            "Editing",
            'file_path="src/app.py"',
            tool_name="edit",
            tool_call_id="edit-1",
            raw_args={"file_path": "src/app.py"},
        )
        request_header = tool.header
        request_body = list(tool.body_lines)
        result = test_dock.append_file_change(
            "\n".join(
                [
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    "@@ -1 +1,1 @@",
                    "-old",
                    "+new",
                ]
            ),
            parent=tool,
            tool_call_id="edit-1",
        )

        assert result is not None
        assert result is not tool
        assert result.parent is tool.parent
        assert tool.header == request_header
        assert tool.body_lines == request_body
        assert result.payload["lifecycle"] == "completed"
        assert result.payload["diff_text"]
        assert result.header.startswith("[dim]●[/dim]")
    finally:
        test_dock.deactivate()
        test_dock.reset()




def test_multi_file_diff_keeps_request_unchanged_and_one_trailing_spacer():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("update two files")
        tool = test_dock.start_tool(
            "Editing",
            'file_path="src/one.py"',
            tool_name="edit",
            tool_call_id="edit-many",
            raw_args={"file_path": "src/one.py"},
        )
        request_header = tool.header
        diff = "\n".join(
            [
                "--- a/src/one.py",
                "+++ b/src/one.py",
                "@@ -1 +1 @@",
                "-one old",
                "+one new",
                "--- a/src/two.py",
                "+++ b/src/two.py",
                "@@ -1 +1 @@",
                "-two old",
                "+two new",
            ]
        )
        first = test_dock.append_file_change(
            diff,
            parent=tool,
            tool_call_id="edit-many",
        )

        results = [
            node
            for node in tool.parent.children
            if node.payload.get("diff_result")
        ]
        spacers = [
            node
            for node in tool.parent.children
            if node.node_type == "message"
            and node.payload.get("tool_result_spacer_for")
        ]
        assert first is results[0]
        assert len(results) == 2
        assert [node.payload["diff_index"] for node in results] == [0, 1]
        assert all(node.parent is tool.parent for node in results)
        assert all(node.payload["lifecycle"] == "completed" for node in results)
        assert tool.header == request_header
        assert len(spacers) == 1
        assert tool.parent.children[tool.parent.children.index(results[-1]) + 1] is spacers[0]
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_duplicate_file_change_event_reuses_results_and_spacer():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("update the file")
        tool = test_dock.start_tool(
            "Editing",
            'file_path="src/app.py"',
            tool_name="edit",
            tool_call_id="edit-repeat",
            raw_args={"file_path": "src/app.py"},
        )
        diff = "\n".join(
            [
                "--- a/src/app.py",
                "+++ b/src/app.py",
                "@@ -1 +1 @@",
                "-old",
                "+new",
            ]
        )
        first = test_dock.append_file_change(
            diff,
            parent=tool,
            tool_call_id="edit-repeat",
        )
        second = test_dock.append_file_change(
            diff,
            parent=tool,
            tool_call_id="edit-repeat",
        )

        results = [
            node for node in tool.parent.children if node.payload.get("diff_result")
        ]
        spacers = [
            node
            for node in tool.parent.children
            if node.node_type == "message"
            and node.payload.get("tool_result_spacer_for")
        ]
        assert second is first
        assert len(results) == 1
        assert len(spacers) == 1
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_tool_result_adds_one_idempotent_trailing_spacer():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("read the file")
        tool = test_dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-1",
            raw_args={"file_path": "src/app.py"},
        )
        result = test_dock.append_tool_result(
            "file contents",
            parent=tool,
            tool_call_id="read-1",
        )
        again = test_dock.append_tool_result(
            "file contents",
            parent=tool,
            tool_call_id="read-1",
        )

        assert result is not None
        assert again is result
        siblings = tool.parent.children
        assert [node for node in siblings if node is result] == [result]
        spacers = [
            node
            for node in siblings
            if node.node_type == "message"
            and not node.header
            and not node.body_lines
            and not node.children
        ]
        assert len(spacers) == 1
        assert siblings[siblings.index(result) + 1] is spacers[0]
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_tool_result_update_reuses_result_and_spacer_without_rewriting_request():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("read the file")
        tool = test_dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-1",
            raw_args={"file_path": "src/app.py"},
        )
        first = test_dock.append_tool_result(
            "first chunk",
            parent=tool,
            tool_call_id="read-1",
        )
        second = test_dock.append_tool_result(
            "second chunk",
            parent=tool,
            tool_call_id="read-1",
        )

        assert first is not None
        assert second is first
        assert first.header == "second chunk"
        assert tool.header.startswith("[#A3BE8C]")
        assert len(
            [
                node
                for node in tool.parent.children
                if node.node_type == "tool_result"
                and node.tool_call_id == "read-1"
            ]
        ) == 1
        assert len(
            [
                node
                for node in tool.parent.children
                if node.node_type == "message"
                and not node.header
                and not node.body_lines
                and not node.children
            ]
        ) == 1
    finally:
        test_dock.deactivate()
        test_dock.reset()


def test_search_started_and_completed_render_as_one_tool_row():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("find compaction summary")
        tool = test_dock.start_tool(
            "Searching",
            'pattern="_compaction_summary"',
            tool_name="search",
            tool_call_id="search-1",
            raw_args={"pattern": "_compaction_summary"},
        )
        started = [_rich_plain(line) for line in test_dock.tree.render(100)]
        assert sum(1 for line in started if 'Search("_compaction_summary")' in line) == 1
        assert test_dock.safe_flush_line_count(100, 0) < len(started)

        test_dock.finish_tool_node(tool, "Search", 0.1, True, "0 matches")
        finished = [_rich_plain(line) for line in test_dock.tree.render(100)]
        search_lines = [line for line in finished if 'Search("_compaction_summary")' in line]

        assert len(search_lines) == 1
        assert "0 matches" in search_lines[0]
        assert test_dock.safe_flush_line_count(100, 0) == len(finished)
        assert finished.count("") <= 1
    finally:
        test_dock.deactivate()
        test_dock.reset()




def test_hidden_tool_display_mode_hides_tool_and_result():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("hidden tool call")
        tool = test_dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-hidden",
            raw_args={"file_path": "src/app.py"},
            display_mode="hidden",
        )
        test_dock.finish_tool_node(tool, "Read", 0.1, True, "done")
        test_dock.append_tool_result(
            "hidden result",
            parent=tool,
            tool_call_id="read-hidden",
        )

        lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        assert not any("Read" in line for line in lines)
        assert not any("hidden result" in line for line in lines)
    finally:
        test_dock.deactivate()
        test_dock.reset()

def test_file_edit_consolidates_into_single_diff_node_without_duplicate_header():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("update file")
        tool = test_dock.start_tool(
            "Editing",
            'file_path="src/app.py"',
            tool_name="replace",
            tool_call_id="replace-1",
            raw_args={"file_path": "src/app.py"},
        )
        test_dock.finish_tool_node(tool, "Update", 0.1, True, "Edited (1 operations)")
        test_dock.append_file_change(
            "\n".join(
                [
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    "@@ -1,2 +1,2 @@",
                    "-old",
                    "+new",
                    " keep",
                ]
            ),
            parent=tool,
            tool_call_id="replace-1",
        )

        lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        update_headers = [line for line in lines if 'Update("src/app.py")' in line]
        assert len(update_headers) == 1
        assert not any("Edited (1 operations)" in line for line in lines)
        assert any("Added 1 line, removed 1 line" in line for line in lines)
    finally:
        test_dock.deactivate()
        test_dock.reset()



def test_assistant_messages_start_after_blank_line_independent_of_previous_node():
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    assistant = tree.new_node(tree.root, node_type="assistant", header="")
    tree.new_node(assistant, node_type="tool_call", header="● Read(\"src/a.py\")")
    tree.new_node(assistant, node_type="assistant", header="读取完成。")
    tree.new_node(assistant, node_type="assistant", header="继续说明。")

    plain_lines = [_rich_plain(line) for line in tree.render(100)]
    tool_index = next(index for index, line in enumerate(plain_lines) if "Read" in line)
    first_message = plain_lines.index("读取完成。")
    second_message = plain_lines.index("继续说明。")

    assert plain_lines[first_message - 1] == ""
    assert first_message == tool_index + 2
    assert plain_lines[second_message - 1] == ""
    assert second_message == first_message + 2



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


def test_text_stream_after_thinking_starts_after_blank_line():
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

        assert plain_lines[answer_index - 1] == ""
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





def test_explicit_show_tool_display_mode_keeps_result_visible():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("show tool output")
        tool = test_dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-show",
            raw_args={"file_path": "src/app.py"},
            display_mode="show",
        )
        test_dock.finish_tool_node(tool, "Read", 0.1, True, "done")
        test_dock.append_tool_result(
            "visible result",
            parent=tool,
            tool_call_id="read-show",
        )

        lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        assert any("visible result" in line for line in lines)
    finally:
        test_dock.deactivate()
        test_dock.reset()



def test_summary_tool_display_mode_hides_raw_result_lines():
    test_dock = dock
    test_dock.begin_capture()
    try:
        test_dock.start_turn("summarize tool output")
        tool = test_dock.start_tool(
            "Searching",
            'query="pattern"',
            tool_name="search",
            tool_call_id="search-summary",
            raw_args={"query": "pattern"},
            display_mode="summary",
        )
        test_dock.finish_tool_node(tool, "Search", 0.1, True, "4 files")
        test_dock.append_tool_result(
            "raw result line 1\nraw result line 2",
            parent=tool,
            tool_call_id="search-summary",
        )

        lines = [_rich_plain(line) for line in test_dock.tree.render(100)]
        assert any("Search" in line and "4 files" in line for line in lines)
        assert not any("raw result line" in line for line in lines)
    finally:
        test_dock.deactivate()
        test_dock.reset()
