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
