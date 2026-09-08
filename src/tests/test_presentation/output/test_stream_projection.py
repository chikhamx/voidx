"""Bounded streaming Markdown projection contracts."""

from __future__ import annotations

from rich.text import Text

from voidx.presentation.output.dock.app import BottomInputDock
from voidx.presentation.output.dock.formatting import text_from_line
from voidx.presentation.output.dock.stream_projection import (
    STREAMING_TAIL_HARD_LIMIT,
    StreamingMarkdownProjection,
)


def _plain(line: str) -> str:
    try:
        return text_from_line(line).plain
    except Exception:
        return Text.from_markup(line).plain


def test_large_single_paragraph_keeps_parser_input_and_mutable_tail_bounded(monkeypatch):
    from voidx.presentation.output.dock import stream_projection as projection_module

    parser_inputs: list[int] = []
    original = projection_module.stable_markdown_prefix_length

    def observed(text: str) -> int:
        parser_inputs.append(len(text))
        return original(text)

    monkeypatch.setattr(projection_module, "stable_markdown_prefix_length", observed)
    projection = StreamingMarkdownProjection(width=76)
    source = "paragraph ".join(str(index) for index in range(6_000))

    update = projection.update(source, operation="append")

    assert len(source) > 50_000
    assert projection.raw_text == source
    assert projection.mutable_tail_length <= STREAMING_TAIL_HARD_LIMIT
    assert max(parser_inputs, default=0) <= STREAMING_TAIL_HARD_LIMIT
    assert update.replace_from == 0
    assert projection.provisional_chunk_count > 0



def test_large_single_segment_preserves_character_and_visual_line_boundaries():
    width = 76
    projection = StreamingMarkdownProjection(width=width)
    source = "a" * 50_123

    projection.update(source, operation="append")

    preview = [_plain(line) for line in projection.preview_lines]
    assert "".join(preview) == source
    assert all(len(line) == width for line in preview[:-1])
    assert len(preview[-1]) == len(source) % width

def test_large_unclosed_fence_is_bounded_and_projected_as_escaped_plain_text(monkeypatch):
    from voidx.presentation.output.dock import stream_projection as projection_module

    markdown_inputs: list[int] = []
    original = projection_module.render_markdown_lines

    def observed(text: str, width: int) -> list[str]:
        markdown_inputs.append(len(text))
        return original(text, width)

    monkeypatch.setattr(projection_module, "render_markdown_lines", observed)
    source = "```text\n" + "[bold]<script>alert('x')</script>[/bold]\n" * 1_300
    projection = StreamingMarkdownProjection(width=76)

    projection.update(source, operation="append")

    assert len(source) > 50_000
    assert projection.raw_text == source
    assert projection.mutable_tail_length <= STREAMING_TAIL_HARD_LIMIT
    assert max(markdown_inputs, default=0) <= STREAMING_TAIL_HARD_LIMIT
    preview = "\n".join(_plain(line) for line in projection.preview_lines)
    assert "[bold]<script>alert('x')</script>[/bold]" in preview
    assert projection.provisional_chunk_count > 0


def test_append_preserves_frozen_preview_prefix_and_only_replaces_mutable_suffix():
    projection = StreamingMarkdownProjection(width=76)

    first = projection.update("# First\n\n", operation="append")
    first_line = projection.preview_lines[0]
    second = projection.update("# Second\n\n", operation="append")

    assert first.replace_from == 0
    assert second.replace_from >= 1
    assert projection.preview_lines[0] is first_line
    preview = "\n".join(_plain(line) for line in projection.preview_lines)
    assert "First" in preview
    assert "Second" in preview


def test_long_list_continuation_stays_mutable_or_escaped_without_losing_raw_text():
    projection = StreamingMarkdownProjection(width=76)
    source = "- item\n" + "  continuation [bold]literal[/bold]\n" * 1_800

    projection.update(source, operation="append")

    assert len(source) > 50_000
    assert projection.raw_text == source
    assert projection.mutable_tail_length <= STREAMING_TAIL_HARD_LIMIT
    assert projection.provisional_chunk_count > 0
    preview = "\n".join(_plain(line) for line in projection.preview_lines)
    assert preview == source.rstrip("\n")


def test_explicit_replace_clears_old_projection_and_preserves_new_canonical_text():
    projection = StreamingMarkdownProjection(width=76)
    projection.update("# Old\n\n" + "x" * 20_000, operation="append")
    old_first_line = projection.preview_lines[0]

    update = projection.update("new [bold]literal[/bold]", operation="replace")

    assert update.replace_from == 0
    assert projection.raw_text == "new [bold]literal[/bold]"
    assert projection.preview_lines[0] is not old_first_line
    preview = "\n".join(_plain(line) for line in projection.preview_lines)
    assert "Old" not in preview
    assert "[bold]literal[/bold]" in preview


def test_resize_rebuilds_from_canonical_raw_text_with_a_bounded_tail():
    projection = StreamingMarkdownProjection(width=80)
    source = "paragraph " * 6_000
    projection.update(source, operation="append")

    update = projection.resize(48)

    assert update.replace_from == 0
    assert projection.width == 48
    assert projection.raw_text == source
    assert projection.mutable_tail_length <= STREAMING_TAIL_HARD_LIMIT



def test_dock_splices_cached_stream_suffix_without_rewalking_frozen_prefix(monkeypatch):
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        source = "a" * 50_123
        dock.set_stream(source, snapshot_contract="delta", refresh=False)
        dock.tree.render(80)
        stream_node = dock._stream_node
        assert stream_node is not None

        original_walk = dock.tree._walk_render

        def reject_stream_rewalk(node, *args, **kwargs):
            if node is stream_node:
                raise AssertionError("cached stream updates must splice only the changed suffix")
            return original_walk(node, *args, **kwargs)

        monkeypatch.setattr(dock.tree, "_walk_render", reject_stream_rewalk)
        dock.set_stream("b" * 76, snapshot_contract="delta", refresh=False)

        rendered = dock.tree.render(80)

        assert any("b" * 20 in _plain(line) for line in rendered[-3:])
        assert stream_node.payload["raw_text"] == source + "b" * 76
    finally:
        dock.deactivate()

def test_dock_accepts_cumulative_updates_and_resets_preview_on_phase_switch():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.set_stream("thinking [bold]literal[/bold]", phase="thinking")
        dock.set_stream("answer", phase="text", snapshot_contract="cumulative")
        dock.set_stream("answer continued", phase="text", snapshot_contract="cumulative")

        node = dock._stream_node
        assert node is not None
        assert node.payload["raw_text"] == "answer continued"
        rendered = "\n".join(_plain(line) for line in dock.tree.render(80))
        assert "thinking" not in rendered
        assert "answer continued" in rendered
    finally:
        dock.deactivate()


def test_dock_accepts_delta_updates_without_requiring_cumulative_snapshots():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.set_stream("● hello", phase="text", snapshot_contract="delta")
        dock.set_stream(" **world**", phase="text", snapshot_contract="delta")

        work_item = dock.prepare_stream_commit(refresh=False)

        assert work_item is not None
        assert work_item.raw_text == "hello **world**"
    finally:
        dock.deactivate()

def test_dock_delta_chunks_preserve_interior_newline_boundaries():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.set_stream("● hello\n", phase="text", snapshot_contract="delta")
        dock.set_stream("world", phase="text", snapshot_contract="delta")

        work_item = dock.prepare_stream_commit(refresh=False)

        assert work_item is not None
        assert work_item.raw_text == "hello\nworld"
    finally:
        dock.deactivate()



def test_dock_preserves_assistant_bullet_at_start_of_later_delta():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.set_stream("● first line\n", phase="text", snapshot_contract="delta")
        dock.set_stream("● literal bullet", phase="text", snapshot_contract="delta")

        work_item = dock.prepare_stream_commit(refresh=False)

        assert work_item is not None
        assert work_item.raw_text == "first line\n● literal bullet"
    finally:
        dock.deactivate()


def test_consecutive_stream_without_intervening_visible_output_consolidates_in_place():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.start_turn("Goal: test consolidation")

        # Step 1: Initial draft stream and commit
        draft_text = "• 已完成长对话 CPU 100% 性能瓶颈的测试用例编写、根因修复与全量验证。\n### 一、根本原因分析与复现"
        dock.set_stream(draft_text, refresh=False)
        dock.commit_stream(refresh=False)

        agent = dock.ensure_agent()
        assistant_nodes_1 = [child for child in agent.children if child.node_type == "assistant"]
        assert len(assistant_nodes_1) == 1

        # Step 2: Second stream with extended/updated text in the same turn without visible tools
        full_text = (
            "• 已完成长对话 CPU 100% 性能瓶颈的测试用例编写、根因修复与全量验证。\n"
            "### 一、根本原因分析与复现\n"
            "通过对长会话中占满 100% CPU 的后台进程采样分析，发现以下三个瓶颈叠\n"
            "1. OutputNode 数据类深度值比较\n"
            "2. mark_root_turns_committed_through_line O(N^2) 累计渲染"
        )
        dock.set_stream(full_text, refresh=False)
        dock.commit_stream(refresh=False)

        assistant_nodes_2 = [child for child in agent.children if child.node_type == "assistant"]
        # Must consolidate in-place into a single assistant node instead of creating a duplicate
        assert len(assistant_nodes_2) == 1
        lines = [line for line in dock.tree.render(80) if "已完成长对话" in line]
        assert len(lines) == 1
    finally:
        dock.deactivate()


def test_consecutive_streams_separated_by_visible_tool_remain_separate():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.start_turn("Goal: test separate streams")

        # Step 1: First stream and commit
        dock.set_stream("First assistant response before tool", refresh=False)
        dock.commit_stream(refresh=False)

        # Step 2: Visible tool execution
        tool = dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-1",
            raw_args={"file_path": "src/app.py"},
        )
        dock.finish_tool_node(tool, "Read", 0.1, True, "10 lines")

        # Step 3: Second stream after visible tool
        dock.set_stream("Second assistant response after tool", refresh=False)
        dock.commit_stream(refresh=False)

        agent = dock.ensure_agent()
        assistant_nodes = [child for child in agent.children if child.node_type == "assistant"]
        # Separated by a visible tool, so these should be two distinct assistant nodes
        assert len(assistant_nodes) == 2
    finally:
        dock.deactivate()



def test_consecutive_stream_with_same_first_line_but_different_body_keeps_nodes_separate():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.start_turn("Goal: preserve distinct stream messages")
        dock.set_stream("same heading\nfirst body", refresh=False)
        dock.commit_stream(refresh=False)
        dock.set_stream("same heading\nsecond body", refresh=False)
        dock.commit_stream(refresh=False)

        assistants = [
            child
            for child in dock.ensure_agent().children
            if child.node_type == "assistant"
        ]
        assert len(assistants) == 2
        assert assistants[0].payload["raw_text"] == "same heading\nfirst body"
        assert assistants[1].payload["raw_text"] == "same heading\nsecond body"
    finally:
        dock.deactivate()



def test_settled_stream_node_is_not_reused_for_later_stream_extension():
    dock = BottomInputDock()
    dock.begin_capture()
    try:
        dock.start_turn("Goal: protect settled stream history")
        dock.set_stream("stable heading\nfirst body", refresh=False)
        dock.commit_stream(refresh=False)
        first = next(
            child
            for child in dock.ensure_agent().children
            if child.node_type == "assistant"
        )
        dock._settled_node_ids.add(first.id)

        dock.set_stream("stable heading\nfirst body\nextension", refresh=False)
        dock.commit_stream(refresh=False)

        assistants = [
            child
            for child in dock.ensure_agent().children
            if child.node_type == "assistant"
        ]
        assert len(assistants) == 2
        assert assistants[0] is first
        assert assistants[0].payload["raw_text"] == "stable heading\nfirst body"
        assert assistants[1].payload["raw_text"] == "stable heading\nfirst body\nextension"
    finally:
        dock.deactivate()
