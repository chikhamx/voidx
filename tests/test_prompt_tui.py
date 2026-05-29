import sys
import re
import asyncio
import pytest
from types import SimpleNamespace

sys.path.insert(0, "src")

from prompt_toolkit.data_structures import Point
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from voidx.ui.app import McpServerStatus, PromptToolkitTui, UiStatus, _STYLE, _continuation_prefix
from voidx.ui.commands import COMMANDS
from voidx.ui.console import StreamingRenderer
from voidx.ui.dock import ANSI_LINE_PREFIX, dock, set_dock, BottomInputDock
from voidx.ui.capture import CaptureConsole
from voidx.ui.tree import OutputTree
from voidx.agent.slash import SlashHandler
from voidx.agent.slash_parts.runtime import ui as slash_ui
from voidx.ui.app_parts.clipboard_image import ClipboardImageResult
from rich.console import Console

@pytest.fixture(autouse=True)
def setup_dock():
    set_dock(BottomInputDock())
    yield
    set_dock(None)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line.replace(ANSI_LINE_PREFIX, ""))


def _scrollbar_button_rows(fragments: list[tuple[str, str]]) -> list[int]:
    rows: list[int] = []
    row = 0
    for style, text in fragments:
        for char in text:
            if char == "\n":
                row += 1
            elif "scrollbar.button" in style:
                rows.append(row)
    return rows


def _tui(
    *,
    mcp_servers=None,
    mcp_config_path: str = "",
    commands: list[tuple[str, str]] | None = None,
    workspace: str = "/tmp/workspace",
) -> PromptToolkitTui:
    return PromptToolkitTui(
        UiStatus(
            provider="provider",
            model="model",
            workspace=workspace,
            session_title="session",
            context_limit=128_000,
            debug=lambda: True,
            plan_mode=lambda: False,
            mcp_servers=mcp_servers or (lambda: []),
            mcp_config_path=mcp_config_path,
        ),
        commands or COMMANDS,
    )


def test_ctrl_c_requires_second_empty_press():
    tui = _tui()

    tui._handle_ctrl_c()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"

    tui._handle_ctrl_c()

    assert tui._queue.get_nowait() is None


def test_ctrl_c_clears_input_before_arming_exit():
    tui = _tui()
    tui.input.text = "hello"

    tui._handle_ctrl_c()

    assert tui.input.text == ""
    assert tui._queue.empty()
    assert "Input cleared" in tui._notice

    tui._handle_ctrl_c()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"


def test_typing_after_ctrl_c_resets_exit_prompt():
    tui = _tui()

    tui._handle_ctrl_c()
    tui.input.text = "hello"

    assert tui._queue.empty()
    assert tui._notice == ""
    assert not tui._ctrl_c_armed


def test_footer_fits_default_width():
    tui = _tui()

    footer = tui._render_footer()
    text = "".join(part[1] for part in footer)

    assert len(text) <= tui._width()


def test_choice_footer_fits_default_width():
    tui = _tui()
    tui._choice_prompt = "Allow tools with a very long explanatory prompt?"
    tui._active_choice = [
        ("Always and remember this setting", "a", ""),
        ("Once for this operation", "y", ""),
        ("No, deny these tools", "n", ""),
    ]

    footer = tui._render_footer()
    text = "".join(part[1] for part in footer)

    assert len(text) <= tui._width()


def test_choice_panel_keeps_selected_row_visible():
    tui = _tui()
    tui._choice_prompt = "Provider"
    tui._active_choice = [(f"provider{index}", str(index), "") for index in range(12)]
    tui._choice_selected = 10

    panel = "".join(text for _, text in tui._render_choice_panel())

    assert "provider0" not in panel
    assert "❯ provider10" in panel
    assert "... 4 above" in panel
    assert "╰" in panel


def test_input_area_matches_body_black_background():
    body_attrs = _STYLE.get_attrs_for_style_str("class:body")
    input_attrs = _STYLE.get_attrs_for_style_str("class:input")

    assert body_attrs.bgcolor == "000000"
    assert input_attrs.bgcolor == body_attrs.bgcolor
    assert input_attrs.color == body_attrs.color == "ECEFF4"


def test_input_area_defaults_to_two_rows():
    tui = _tui()
    height = tui.input.window.height

    assert height.min == 2
    assert height.preferred == 2
    assert height.max == 2


def test_slash_command_panel_renders_without_polluting_transcript():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("existing transcript")
        tui = _tui()
        tui.input.text = "/"

        panel = "".join(text for _, text in tui._render_command_panel())
        body = "".join(text for _, text in tui._render_body())
        footer = "".join(text for _, text in tui._render_footer())

        assert "Slash commands" in panel
        assert "/mcp" in panel
        assert "Manage MCP servers" in panel
        assert "Slash commands" not in body
        assert "↑/↓ select" in footer
    finally:
        dock.deactivate()
        dock.reset()


def test_attachment_panel_accepts_workspace_file(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")
    tui = _tui(workspace=str(tmp_path))
    tui.input.text = "@src"
    tui.input.buffer.cursor_position = len(tui.input.text)

    assert tui._attachment_panel_active()
    panel = "".join(text for _, text in tui._render_attachment_panel())
    assert "src/main.py" in panel

    assert tui._accept_attachment_panel_selection()
    assert tui.input.text == "@src/main.py "


def test_attachment_panel_quotes_paths_with_spaces(tmp_path):
    file_path = tmp_path / "notes" / "my file.txt"
    file_path.parent.mkdir()
    file_path.write_text("hello\n", encoding="utf-8")
    tui = _tui(workspace=str(tmp_path))
    tui.input.text = "@my"
    tui.input.buffer.cursor_position = len(tui.input.text)

    assert tui._accept_attachment_panel_selection()
    assert tui.input.text == '@"notes/my file.txt" '


def test_attachment_panel_keeps_selected_row_visible(tmp_path):
    for index in range(8):
        (tmp_path / f"file{index}.txt").write_text(str(index), encoding="utf-8")
    tui = _tui(workspace=str(tmp_path))
    tui.input.text = "@file"
    tui.input.buffer.cursor_position = len(tui.input.text)
    tui._attachment_selected = 7

    panel = "".join(text for _, text in tui._render_attachment_panel())

    assert "file0.txt" not in panel
    assert "❯ file7.txt" in panel


def test_paste_clipboard_image_inserts_attachment_token(tmp_path, monkeypatch):
    def fake_paste(workspace: str):
        assert workspace == str(tmp_path)
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=123,
        )

    monkeypatch.setattr("voidx.ui.app.paste_clipboard_image_from_system", fake_paste)
    tui = _tui(workspace=str(tmp_path))
    tui.input.text = "describe"
    tui.input.buffer.cursor_position = len(tui.input.text)

    result = tui.paste_clipboard_image()

    assert result.ok
    assert tui.input.text == "describe @.voidx/attachments/clip.png "
    assert tui._notice == "Pasted image"


def test_paste_command_inserts_clipboard_image_without_queueing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "voidx.ui.app.paste_clipboard_image_from_system",
        lambda _workspace: ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=123,
        ),
    )
    tui = _tui(workspace=str(tmp_path))
    tui.input.text = "/paste"
    tui.input.buffer.cursor_position = len(tui.input.text)

    tui._submit_input()

    assert tui.input.text == "@.voidx/attachments/clip.png "
    assert tui._queue.empty()


def test_mcp_command_panel_renders_server_status_like_claude():
    tui = _tui(
        mcp_config_path="/tmp/voidx.json",
        mcp_servers=lambda: [
            McpServerStatus(name="web-reader", status="connected", tool_count=1, source="User MCPs"),
            McpServerStatus(name="zai-mcp-server", status="connected", tool_count=8, source="User MCPs"),
        ],
    )
    tui.input.text = "/mcp"

    panel = "".join(text for _, text in tui._render_command_panel())

    assert "Manage MCP servers" in panel
    assert "2 servers" in panel
    assert "User MCPs" in panel
    assert "/tmp/voidx.json" in panel
    assert "❯ web-reader" in panel
    assert "✓ connected" in panel
    assert "1 tool" in panel
    assert "8 tools" in panel


def test_command_panel_completes_partial_command_before_submit():
    tui = _tui()
    tui.input.text = "/mc"

    handled = tui._accept_command_panel_selection()

    assert handled is True
    assert tui.input.text == "/mcp"
    assert tui._queue.empty()


@pytest.mark.asyncio
async def test_text_prompt_uses_input_without_main_submit_queue():
    tui = _tui()
    task = asyncio.create_task(tui.ask_text("API key", secret=True))
    await asyncio.sleep(0)

    assert tui._active_text_prompt == "API key"
    assert tui._active_text_secret is True

    tui.input.text = "sk-test"
    tui._submit_text_prompt()

    assert await task == "sk-test"
    assert tui._queue.empty()
    assert tui._active_text_prompt is None


def test_command_output_panel_renders_without_polluting_transcript():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("existing transcript")
        tui = _tui()

        tui.begin_command_output("/help")
        tui.append_command_output("Commands:\n  /help  Show all commands")

        panel = "".join(text for _, text in tui._render_command_output_panel())
        body = "".join(text for _, text in tui._render_body())

        assert "Command Output" in panel
        assert "/help" in panel
        assert "/help" not in body
        assert tui._command_output_bottom_active()

        tui._width = lambda: 140

        assert tui._command_output_wide_active()
        assert tui._main_width() < tui._width()
    finally:
        dock.deactivate()
        dock.reset()


@pytest.mark.asyncio
async def test_slash_command_output_capture_avoids_transcript():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tui = _tui()
        tui.begin_command_output("/help")

        with slash_ui.capture_command_output(
            tui.append_command_output,
            width=tui.command_output_width,
        ):
            await SlashHandler(SimpleNamespace()).dispatch("/help")

        panel = "".join(text for _, text in tui._render_command_output_panel())
        body = "".join(text for _, text in tui._render_body())

        assert "Commands:" in panel
        assert "/clear" in panel
        assert "Commands:" not in body
        assert "/clear" not in body
    finally:
        dock.deactivate()
        dock.reset()


def test_permission_choice_panel_renders_tool_details_without_body_text():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("existing transcript")
        tui = _tui()
        tui._choice_prompt = "Allow tool use?"
        tui._active_choice = [
            ("Yes, always", "a", "Allow these tools for this session"),
            ("Yes", "y", "Allow once"),
            ("No", "n", "Deny"),
        ]
        tui._choice_details = [
            {
                "name": "bash",
                "pattern": "npm test",
                "args": {"command": "npm test", "timeout": 120},
            }
        ]

        panel = "".join(text for _, text in tui._render_choice_panel())
        body = "".join(text for _, text in tui._render_body())

        assert "Allow tool use?" in panel
        assert "bash" in panel
        assert "npm test" in panel
        assert "Yes, and don't ask again this session" in panel
        assert "npm test" not in body
    finally:
        dock.deactivate()
        dock.reset()


def _mouse_event(event_type: MouseEventType, x: int = 0, y: int = 0) -> MouseEvent:
    return MouseEvent(Point(x=x, y=y), event_type, MouseButton.NONE, frozenset())


def test_prompt_tui_routes_mouse_events_inside_app():
    tui = _tui()

    assert tui.app.mouse_support() is True
    assert "F2" not in tui._hint_text()


def test_input_mouse_events_are_ignored_and_do_not_scroll_transcript():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("\n".join(f"line {i}" for i in range(40)))
        tui = _tui()

        handled = tui.input.control.mouse_handler(
            _mouse_event(MouseEventType.SCROLL_UP, x=4, y=1)
        )

        assert handled is None
        assert tui._scroll_offset == 0
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_mouse_wheel_scrolls_body_region():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("\n".join(f"line {i}" for i in range(40)))
        tui = _tui()

        handled = tui.body_control.mouse_handler(
            _mouse_event(MouseEventType.SCROLL_UP, x=4, y=1)
        )

        assert handled is None
        assert tui._scroll_offset == 3

        tui.body_control.mouse_handler(_mouse_event(MouseEventType.SCROLL_DOWN, x=4, y=1))

        assert tui._scroll_offset == 0
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_mouse_click_is_handled_by_body_region():
    tui = _tui()

    handled = tui.body_control.mouse_handler(
        _mouse_event(MouseEventType.MOUSE_DOWN, x=7, y=2)
    )

    assert handled is None
    assert tui._last_body_click == Point(x=7, y=2)


def test_transcript_click_toggles_collapsed_tool_result():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("demo")
        tool = dock.start_tool("Reading", "")
        result = dock.append_tool_result("first\nsecond", parent=tool, collapsed=True)
        tui = _tui()
        tui._render_body()

        row = next(i for i, line in enumerate(tui._visible_body_lines) if "first" in _plain(line))
        tui._toggle_body_node_at(row)

        assert result is not None
        assert result.collapsed is False
        rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
        assert "second" in rendered

        tui._render_body()
        tui._toggle_body_node_at(row)

        assert result.collapsed is True
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_has_no_keyboard_scroll_bindings():
    tui = _tui()
    blocked = {Keys.PageUp, Keys.PageDown, Keys.Home, Keys.End}
    bound = {binding.keys[0] for binding in tui.app.key_bindings.bindings}

    assert blocked.isdisjoint(bound)


def test_prompt_tui_wraps_transcript_lines():
    tui = _tui()
    body = tui.app.layout.container.children[0].children[0]

    assert body.wrap_lines() is True


def test_transcript_continuation_prefix_preserves_left_alignment():
    assert _continuation_prefix("  • ANSI 处理 很长的一行") == "    "
    assert _continuation_prefix("    Rich markup 和原始 ANSI") == "    "
    assert _continuation_prefix(" [dim]├─[/dim] [bold]Reading[/](file_path=\"long\")") == "    "


def test_transcript_scrollbar_tracks_manual_scroll_offset():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("\n".join(f"line {i}" for i in range(40)))
        tui = _tui()

        bottom_rows = _scrollbar_button_rows(tui._render_scrollbar_margin(10))
        tui._scroll_offset = 30
        top_rows = _scrollbar_button_rows(tui._render_scrollbar_margin(10))

        assert min(top_rows) < min(bottom_rows)
        assert max(bottom_rows) == 9
        assert min(top_rows) == 0
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_scroll_to_top_shows_startup_header_without_clipping():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_startup(
            model="mimo-v2.5",
            provider="mimo",
            workspace="/Users/chikham/workspace/voidx",
            session_title="你好",
            is_new=False,
        )
        for i in range(30):
            dock.append_message(f"line {i}")

        tui = _tui()
        tui._body_height = lambda: 6
        tui._scroll_to_top()
        tui._render_body()

        visible = "\n".join(_plain(line) for line in tui._visible_body_lines)

        assert "╭─ voidx v" in _plain(tui._visible_body_lines[0])
        assert "Ask anything" in visible
        assert "lines below" not in visible
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_sets_cursor_on_last_visible_line_for_wrapped_scroll():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("first")
        dock.append_message("tail")
        tui = _tui()
        content = tui.body_control.create_content(width=20, height=3)

        assert content.cursor_position is not None
        assert content.cursor_position.y == content.line_count - 1
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_stream_renders_markdown_to_ansi():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.set_stream("● 这是 **voidx**\n\n- **多 LLM 支持**")
        rendered = "\n".join(dock.tree.render(80))

        assert ANSI_LINE_PREFIX in rendered
        assert "\x1b[" in rendered
        assert "**voidx**" not in rendered
        assert "**多 LLM 支持**" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_capture_preserves_rich_styles_as_ansi():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.capture(lambda console: console.print("[dim]dim text[/dim]"))
        rendered = "\n".join(dock.tree.render(80))

        assert ANSI_LINE_PREFIX in rendered
        assert "\x1b[" in rendered
        assert "[dim]" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_manual_scroll_updates_offset():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("\n".join(f"line {i}" for i in range(40)))
        tui = _tui()

        tui._scroll_by(3)
        assert tui._scroll_offset == 3

        tui._scroll_by(-3)
        assert tui._scroll_offset == 0
    finally:
        dock.deactivate()
        dock.reset()


def test_transcript_scroll_offset_clamps_to_available_history():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_message("\n".join(f"line {i}" for i in range(40)))
        tui = _tui()

        tui._scroll_by(10_000)

        assert tui._scroll_offset == tui._max_scroll()

        tui._scroll_by(-10_000)

        assert tui._scroll_offset == 0
    finally:
        dock.deactivate()
        dock.reset()


def test_final_answer_renders_after_tool_nodes():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("这个项目是什么")
        tool = dock.start_tool("Mapping", "")
        dock.finish_tool_node(tool, "repo_map", 0.0, True)
        dock.append_tool_result("src/voidx/agent/agents.py", parent=tool, collapsed=False)

        dock.set_stream("● 项目结构清晰，支持 **Markdown**。")

        lines = [_plain(line) for line in dock.tree.render(100)]
        tool_index = next(i for i, line in enumerate(lines) if "Mapping" in line)
        answer_index = next(i for i, line in enumerate(lines) if "项目结构清晰" in line)

        assert tool_index < answer_index
        assert lines[answer_index].startswith("● ")
    finally:
        dock.deactivate()
        dock.reset()


def test_tool_result_does_not_duplicate_first_line_when_expanded():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool("Reading", "")
        result = dock.append_tool_result("first\nsecond", parent=tool, collapsed=False)

        assert result is not None
        assert result.header == "first"
        assert result.body_lines == ["second"]
    finally:
        dock.deactivate()
        dock.reset()


def test_tool_result_renders_as_compact_block_under_tool():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool("Reading", "")
        dock.finish_tool_node(tool, "read", 0.0, True)
        dock.append_tool_result("first\nsecond", parent=tool, collapsed=False)

        lines = [_plain(line) for line in dock.tree.render(100)]
        tool_index = next(i for i, line in enumerate(lines) if "Reading" in line)

        assert lines[tool_index + 1].strip() == "first"
        assert lines[tool_index + 2].strip() == "second"
    finally:
        dock.deactivate()
        dock.reset()


def test_file_change_renders_claude_style_update_node():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool("Editing", 'file_path="test.cpp"')
        dock.finish_tool_node(tool, "edit", 0.0, True)
        dock.append_file_change(
            """--- a/test.cpp
+++ b/test.cpp
@@ -1,2 +1,2 @@
-old
+new
 keep
""",
            parent=tool,
        )

        rendered = "\n".join(_plain(line) for line in dock.tree.render(120))

        assert "Update" in rendered
        assert "(test.cpp)" in rendered
        assert "Added 1 line, removed 1 line" in rendered
        assert "1 -" in rendered
        assert "old" in rendered
        assert "1 +" in rendered
        assert "new" in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tree_renders_nested_connectors():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("demo")
        tool = dock.start_tool("Reading", "")
        dock.finish_tool_node(tool, "read", 0.0, True)
        dock.append_tool_result("first\nsecond", parent=tool, collapsed=False)

        rendered = "\n".join(_plain(line) for line in dock.tree.render(100))

        assert "└─" in rendered
        assert "Reading" in rendered
        assert "first" in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tree_connector_only_marks_first_sibling():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("demo")
        first = dock.start_tool("Reading", "")
        dock.finish_tool_node(first, "read", 0.0, True)
        dock.append_tool_result("first", parent=first, collapsed=False)
        second = dock.start_tool("Finding", "")
        dock.finish_tool_node(second, "glob", 0.0, True)
        dock.append_tool_result("second", parent=second, collapsed=False)
        third = dock.start_tool("Reading", "")
        dock.finish_tool_node(third, "read", 0.0, True)
        dock.append_tool_result("third", parent=third, collapsed=False)

        rendered = "\n".join(dock.tree.render(100))
        connector_count = rendered.count("├─") + rendered.count("└─")

        assert "[dim]├─[/dim]" in rendered
        assert connector_count == 1
        assert "│" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_spacing_is_root_level_blank_line():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        first = dock.start_turn("one")
        dock.start_turn("two")

        lines = [_plain(line) for line in dock.tree.render(100)]
        assert "one" in lines[0]
        assert lines[1] == ""
        assert "two" in lines[2]
        assert first.body_lines == []
    finally:
        dock.deactivate()
        dock.reset()


def test_capture_console_tool_result_does_not_duplicate_first_line():
    tree = OutputTree()
    parent = tree.new_node(tree.root, node_type="assistant", header="agent")
    capture = CaptureConsole(tree, parent)

    capture.tool_result("first\nsecond")

    result = parent.children[0]
    assert result.header == "first"
    assert result.body_lines == ["second"]


def test_tool_start_replaces_partial_preamble_with_working_label():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("demo")
        dock.set_stream("● Let me take a quick look at the")

        dock.start_tool("Reading", "")

        rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
        assert "Let me take a quick look" not in rendered
        assert "Working" in rendered
        assert "Reading" in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_stream_markdown_uses_prompt_toolkit_width_provider():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    dock.set_width_provider(lambda: 140)
    try:
        text = "● " + "word " * 22
        dock.set_stream(text)
        agent = dock.current_agent

        assert agent is not None
        assert agent.body_lines == []
    finally:
        dock.set_width_provider(None)
        dock.deactivate()
        dock.reset()


def test_stream_markdown_drops_rich_blank_lines_between_blocks():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.set_stream("● intro\n\n\n- one\n\n- two")
        agent = dock.current_agent

        assert agent is not None
        lines = [_plain(agent.header), *[_plain(line) for line in agent.body_lines]]
        assert [line.strip() for line in lines] == ["● intro", "• one", "• two"]
        assert all(line.strip() for line in lines)
    finally:
        dock.deactivate()
        dock.reset()


def test_stream_markdown_body_lines_align_under_answer_text():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.set_stream("● 这是 voidx\n技术栈:\n- LangGraph\n- Rich")
        agent = dock.current_agent

        assert agent is not None
        assert all(_plain(line).startswith("  ") for line in agent.body_lines)
        assert _plain(agent.body_lines[0]).startswith("   • LangGraph")

        rendered = [_plain(line) for line in dock.tree.render(100)]
        assert rendered[1].startswith("   • LangGraph")
        assert rendered[2].startswith("   • Rich")
    finally:
        dock.deactivate()
        dock.reset()


def test_stream_markdown_preserves_wrapped_tail_with_width_budget():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    dock.set_width_provider(lambda: 36)
    try:
        dock.set_stream(
            "● 这是一个非常非常非常非常非常非常非常非常非常非常非常非常长的中文 Markdown "
            "段落，用来检查尾部内容是否还在。尾部标记XYZ"
        )
        agent = dock.current_agent

        assert agent is not None
        lines = [_plain(agent.header), *[_plain(line) for line in agent.body_lines]]
        assert len(lines) > 1
        assert "尾部标记XYZ" in "".join(line.strip() for line in lines)
        assert all("\n" not in line for line in [agent.header, *agent.body_lines])
    finally:
        dock.set_width_provider(None)
        dock.deactivate()
        dock.reset()


def test_streaming_renderer_flushes_final_throttled_markdown():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        renderer = StreamingRenderer(Console(), debug=True)
        renderer.feed_text(
            "这是一个叫 **voidx** 的终端 AI 编码助手项目，主要特性包括：\n\n"
        )
        renderer.feed_text("- **多 LLM 支持**\n- **多 agent 架构**")

        rendered = "\n".join(_plain(line) for line in dock.tree.render(120))
        assert "多 LLM 支持" not in rendered

        renderer.done()

        rendered = "\n".join(_plain(line) for line in dock.tree.render(120))
        assert "多 LLM 支持" in rendered
        assert "多 agent 架构" in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_markdown_rendering_strips_trailing_fill_spaces():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    dock.set_width_provider(lambda: 100)
    try:
        dock.set_stream("● 1. 用户截图 / 复制图片到剪贴板\n\n2. 在 voidx 输入框按 Ctrl+V")
        agent = dock.current_agent

        assert agent is not None
        lines = [agent.header, *agent.body_lines]
        assert all(not _plain(line).endswith(" ") for line in lines)
    finally:
        dock.set_width_provider(None)
        dock.deactivate()
        dock.reset()


def test_markdown_rendering_strips_background_ansi():
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.append_ansi("\x1b[48;5;238mstriped\x1b[0m")
        rendered = "\n".join(dock.tree.render(80))

        assert "48;5;238" not in rendered
        assert "striped" in rendered
    finally:
        dock.deactivate()
        dock.reset()
