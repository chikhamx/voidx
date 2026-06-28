"""Tests for file coverage fingerprint and replace/line tool integration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
import voidx.tools.file_state as file_state

class TestFileOps:
    @pytest.mark.asyncio
    async def test_read_coverage_uses_mtime_ns_and_size_fingerprint(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "fp.txt"
        f.write_text("hello\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "fp.txt"}, ctx)

        key = str((tmp_path / "fp.txt").resolve())
        coverage = ctx.file_read_coverage[key]
        fp = fs.file_fingerprint(tmp_path / "fp.txt")
        assert coverage["fingerprint"] == {"mtime_ns": fp.mtime_ns, "size": fp.size}

        result = await r.execute_tool(
            "replace",
            {"file_path": "fp.txt", "start_no": 1, "end_no": 1, "start_anchor": "hello", "end_anchor": "hello", "new_string": "HELLO"},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_then_replace_handles_shifted_lines(self, tmp_path):
        f = tmp_path / "paragraph.py"
        f.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "    value = 2\n"
            "    return value\n"
        )
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph.py"}, ctx)

        shifted = await r.execute_tool(
            "write",
            {"file_path": "paragraph.py", "op": "insert", "lineno": 2, "new_string": "    extra = 0\n"},
            ctx,
        )
        corrected = await r.execute_tool(
            "replace",
            {"file_path": "paragraph.py", "start_no": 5, "end_no": 5, "start_anchor": "def bar():", "end_anchor": "def bar():", "new_string": "def baz():"},
            ctx,
        )

        assert shifted.metadata.get("error") is not True
        assert corrected.metadata.get("error") is not True
        assert "def baz():\n    value = 2" in (tmp_path / "paragraph.py").read_text()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_reports_ambiguous_and_missing_matches(self, tmp_path):
        f = tmp_path / "paragraph-errors.py"
        f.write_text("target = 1\nother = 0\ntarget = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-errors.py"}, ctx)

        ambiguous = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-errors.py", "start_no": 2, "end_no": 2, "start_anchor": "target", "end_anchor": "target", "new_string": "changed"},
            ctx,
        )
        missing = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-errors.py", "start_no": 2, "end_no": 2, "start_anchor": "missing", "end_anchor": "missing", "new_string": "changed"},
            ctx,
        )

        assert "ambiguous" in ambiguous.output.lower()
        assert ambiguous.metadata.get("error")
        assert "not found" in missing.output.lower()
        assert missing.metadata.get("error")
        assert (tmp_path / "paragraph-errors.py").read_text() == "target = 1\nother = 0\ntarget = 2\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_lineno_hint_disambiguates_nearest_prefix(self, tmp_path):
        f = tmp_path / "nearest.py"
        f.write_text("def item():\n    a = 1\n\ndef item():\n    a = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "nearest.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "nearest.py", "start_no": 4, "end_no": 5, "start_anchor": "def item():", "end_anchor": "a = 2", "new_string": "def item():\n    a = 3"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "nearest.py").read_text() == "def item():\n    a = 1\n\ndef item():\n    a = 3\n"

    @pytest.mark.asyncio
    async def test_replace_multiline_prefix_replaces_multiline_range(self, tmp_path):
        f = tmp_path / "multi-line-prefix.py"
        f.write_text("top\ninserted\nstart\nmiddle\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-line-prefix.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi-line-prefix.py", "start_no": 2, "end_no": 4, "start_anchor": "inserted", "end_anchor": "middle", "new_string": "START\nMIDDLE"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "multi-line-prefix.py").read_text() == "top\nSTART\nMIDDLE\nend\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_after_shifted_line(self, tmp_path):
        f = tmp_path / "insert-paragraph-correct.py"
        f.write_text("top\ninserted\ntarget\nbottom\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-paragraph-correct.py"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-paragraph-correct.py", "op": "insert", "lineno": 3, "new_string": "after target\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "insert-paragraph-correct.py").read_text() == "top\ninserted\ntarget\nafter target\nbottom\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_still_requires_read_coverage(self, tmp_path):
        f = tmp_path / "paragraph-coverage.py"
        f.write_text("top\nmiddle\ntarget\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-coverage.py", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-coverage.py", "start_no": 1, "end_no": 1, "start_anchor": "target", "end_anchor": "target", "new_string": "TARGET"},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "paragraph-coverage.py").read_text() == "top\nmiddle\ntarget\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_replace_on_same_file(self, tmp_path):
        f = tmp_path / "paragraph-conflict.py"
        f.write_text("top\ntarget\nbottom\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-conflict.py"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-conflict.py", "start_no": 2, "end_no": 2, "start_anchor": "target", "end_anchor": "target", "new_string": "TARGET"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-conflict.py", "start_no": 1, "end_no": 1, "start_anchor": "top", "end_anchor": "top", "new_string": "TOP"},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert (tmp_path / "paragraph-conflict.py").read_text() == "TOP\nTARGET\nbottom\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_and_replace_delete_report_line_shift(self, tmp_path):
        f = tmp_path / "shift.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "shift.txt"}, ctx)

        inserted = await r.execute_tool(
            "write",
            {"file_path": "shift.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )
        deleted = await r.execute_tool(
            "replace",
            {"file_path": "shift.txt", "start_no": 3, "end_no": 3, "start_anchor": "two", "end_anchor": "two", "new_string": ""},
            ctx,
        )

        assert inserted.metadata.get("error") is not True
        assert "shift" in inserted.output.lower()
        assert deleted.metadata.get("error") is not True
        assert "shift" in deleted.output.lower()

    @pytest.mark.asyncio
    async def test_replace_same_line_count_does_not_report_shift(self, tmp_path):
        f = tmp_path / "same-lines.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "same-lines.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "same-lines.txt", "start_no": 2, "end_no": 2, "start_anchor": "two", "end_anchor": "two", "new_string": "TWO"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "line shift" not in result.output.lower()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_insert_and_delete_report_line_shifts(self, tmp_path):
        f = tmp_path / "multi-shift.txt"
        f.write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-shift.txt"}, ctx)

        inserted = await r.execute_tool(
            "write",
            {"file_path": "multi-shift.txt", "op": "insert", "lineno": 1, "new_string": "one-a\n"},
            ctx,
        )
        deleted = await r.execute_tool(
            "replace",
            {"file_path": "multi-shift.txt", "start_no": 6, "end_no": 6, "start_anchor": "five", "end_anchor": "five", "new_string": ""},
            ctx,
        )

        assert inserted.metadata.get("error") is not True
        assert "shift" in inserted.output.lower()
        assert deleted.metadata.get("error") is not True
        assert "shift" in deleted.output.lower()

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)
        assert "File not found" in result.output

    @pytest.mark.asyncio
    async def test_read_offset_beyond_file(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "short.txt", "offset": 100}, ctx)
        assert result.metadata["lines"] == 0
        assert "beyond" in result.output.lower() or "offset" in result.output.lower()
        assert result.metadata["error"] is True
        assert result.metadata["reason"] == "offset_beyond_eof"




class TestLineDriftMapModel:
    def test_line_drift_map_round_trip_serialization(self):
        from voidx.tools.file_state import (
            LineDriftMap,
            ReadLineRange,
            DiffSpan,
            _line_drift_maps_from_raw,
            _line_drift_maps_to_raw,
        )

        maps = [
            LineDriftMap(
                epoch=1,
                source_ranges=[ReadLineRange(1, 100)],
                span_steps=[
                    [DiffSpan(old_start=20, old_end=30, offset=-6)],
                    [DiffSpan(old_start=34, old_end=44, offset=-8)],
                ],
            ),
            LineDriftMap(
                epoch=2,
                source_ranges=[ReadLineRange(20, 30)],
                span_steps=[],
            ),
        ]
        raw = _line_drift_maps_to_raw(maps)
        restored = _line_drift_maps_from_raw(raw)

        assert len(restored) == 2
        assert restored[0].epoch == 1
        assert restored[0].source_ranges == [ReadLineRange(1, 100)]
        assert len(restored[0].span_steps) == 2
        assert restored[0].span_steps[0] == [DiffSpan(20, 30, -6)]
        assert restored[0].span_steps[1] == [DiffSpan(34, 44, -8)]
        assert restored[1].epoch == 2
        assert restored[1].source_ranges == [ReadLineRange(20, 30)]
        assert restored[1].span_steps == []

    def test_line_drift_maps_from_raw_empty(self):
        from voidx.tools.file_state import _line_drift_maps_from_raw

        assert _line_drift_maps_from_raw([]) == []
        assert _line_drift_maps_from_raw(None) == []


class TestRecordReadRangePreservesDriftMaps:
    def test_first_read_creates_epoch_1_empty_steps(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 2)

        key = str(f.resolve())
        coverage = ctx.file_read_coverage[key]
        maps = fs._line_drift_maps_from_raw(coverage.get("line_drift_maps"))
        assert len(maps) == 1
        assert maps[0].epoch == 1
        assert maps[0].source_ranges == [fs.ReadLineRange(1, 2)]
        assert maps[0].span_steps == []

    def test_second_read_appends_epoch_2_preserves_epoch_1(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 3)
        fs.record_read_range(ctx, f, 2, 3)

        key = str(f.resolve())
        maps = fs._line_drift_maps_from_raw(ctx.file_read_coverage[key].get("line_drift_maps"))
        assert len(maps) == 2
        assert maps[0].epoch == 1
        assert maps[0].source_ranges == [fs.ReadLineRange(1, 3)]
        assert maps[1].epoch == 2
        assert maps[1].source_ranges == [fs.ReadLineRange(2, 3)]

    def test_fingerprint_mismatch_clears_old_maps(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 2)

        # 模拟文件被外部修改:改内容后 mtime 变化
        import time
        time.sleep(0.01)
        f.write_text("completely different\n")
        fs.record_read_range(ctx, f, 1, 1)

        key = str(f.resolve())
        maps = fs._line_drift_maps_from_raw(ctx.file_read_coverage[key].get("line_drift_maps"))
        assert len(maps) == 1
        assert maps[0].epoch == 1  # 重新从 1 开始

    def test_fifo_eviction_when_exceeding_max(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "a.txt"
        f.write_text("line1\n")
        ctx = ToolContext(workspace=str(tmp_path))
        # 写入 MAX+1 次 read
        for i in range(fs.MAX_LINE_DRIFT_MAPS_PER_FILE + 1):
            fs.record_read_range(ctx, f, 1, 1)

        key = str(f.resolve())
        maps = fs._line_drift_maps_from_raw(ctx.file_read_coverage[key].get("line_drift_maps"))
        assert len(maps) == fs.MAX_LINE_DRIFT_MAPS_PER_FILE
        # epoch 最小的被淘汰,保留 2..MAX+1
        assert maps[0].epoch == 2
        assert maps[-1].epoch == fs.MAX_LINE_DRIFT_MAPS_PER_FILE + 1


class TestRemapAppendsStep:
    def test_edit_appends_step_to_each_map(self, tmp_path):
        import voidx.tools.file_state as fs
        from voidx.diffing import make_structured_diff

        f = tmp_path / "a.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 10)
        fs.record_read_range(ctx, f, 5, 8)

        key = str(f.resolve())
        old_ranges = ctx.file_read_coverage[key]["ranges"]
        # edit: 把 l2..l6 (5行) 替换成 X (1行),偏移 -4
        old_content = f.read_text()
        new_content = "l1\nX\nl7\nl8\nl9\nl10\n"
        f.write_text(new_content)
        file_diff = make_structured_diff("a.txt", old_content, new_content)
        fs.remap_read_coverage_from_file_diff(ctx, f, file_diff, old_ranges=old_ranges)

        maps = fs._line_drift_maps_from_raw(ctx.file_read_coverage[key].get("line_drift_maps"))
        assert len(maps) == 2
        for m in maps:
            assert len(m.span_steps) == 1
            step = m.span_steps[0]
            assert len(step) == 1
            assert step[0].offset == -4

    def test_multiple_edits_without_read_accumulate_steps(self, tmp_path):
        import voidx.tools.file_state as fs
        from voidx.diffing import make_structured_diff

        f = tmp_path / "a.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 10)

        key = str(f.resolve())
        # edit 1: l2..l3 -> X (偏移 -1)
        old1 = f.read_text()
        f.write_text("l1\nX\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        fd1 = make_structured_diff("a.txt", old1, f.read_text())
        old_ranges1 = ctx.file_read_coverage[key]["ranges"]
        fs.remap_read_coverage_from_file_diff(ctx, f, fd1, old_ranges=old_ranges1)

        # edit 2: l5..l6 -> Y (偏移 -1),在 edit1 后的坐标系
        old2 = f.read_text()
        f.write_text("l1\nX\nl4\nY\nl7\nl8\nl9\nl10\n")
        fd2 = make_structured_diff("a.txt", old2, f.read_text())
        old_ranges2 = ctx.file_read_coverage[key]["ranges"]
        fs.remap_read_coverage_from_file_diff(ctx, f, fd2, old_ranges=old_ranges2)

        maps = fs._line_drift_maps_from_raw(ctx.file_read_coverage[key].get("line_drift_maps"))
        assert len(maps) == 1
        assert len(maps[0].span_steps) == 2

    def test_ranges_empty_clears_key_and_maps(self, tmp_path):
        import voidx.tools.file_state as fs
        from voidx.diffing import make_structured_diff

        f = tmp_path / "a.txt"
        f.write_text("l1\nl2\nl3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 3)

        key = str(f.resolve())
        # 删除全部内容,使 ranges 为空
        old_content = f.read_text()
        f.write_text("")
        file_diff = make_structured_diff("a.txt", old_content, "")
        fs.remap_read_coverage_from_file_diff(ctx, f, file_diff, old_ranges=[])

        assert key not in ctx.file_read_coverage


class TestGetLineDriftMaps:
    def test_untracked_file_returns_empty(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "nope.txt"
        ctx = ToolContext(workspace=str(tmp_path))
        assert fs.get_line_drift_maps(ctx, f) == []

    def test_returns_maps_after_read(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "a.txt"
        f.write_text("l1\nl2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 2)

        maps = fs.get_line_drift_maps(ctx, f)
        assert len(maps) == 1
        assert maps[0].epoch == 1
        assert maps[0].source_ranges == [fs.ReadLineRange(1, 2)]
        assert maps[0].span_steps == []

    def test_returns_empty_after_clear(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "a.txt"
        f.write_text("l1\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 1)
        fs.clear_read_coverage(ctx, f)

        assert fs.get_line_drift_maps(ctx, f) == []



class TestRemapLineRange:
    def test_no_steps_returns_original(self):
        from voidx.tools.file_ops.edit_resolve import remap_line_range

        assert remap_line_range(10, 20, []) == (10, 20)

    def test_single_edit_offset(self):
        from voidx.tools.file_ops.edit_resolve import remap_line_range
        from voidx.tools.file_state import DiffSpan

        # edit 20-30 -> 5行,偏移 -6;行号 40 在 edit 之后,应偏移 -6
        steps = [[DiffSpan(20, 30, -6)]]
        assert remap_line_range(40, 50, steps) == (34, 44)

    def test_multiple_edits_accumulate(self):
        from voidx.tools.file_ops.edit_resolve import remap_line_range
        from voidx.tools.file_state import DiffSpan

        # edit1: 20-30 -> 5行 (-6); edit2: 34-44 -> 3行 (-8)
        # 老行号 60: 60 -> 54 (edit1) -> 46 (edit2)
        steps = [
            [DiffSpan(20, 30, -6)],
            [DiffSpan(34, 44, -8)],
        ]
        assert remap_line_range(60, 60, steps) == (46, 46)

    def test_range_fully_deleted_returns_none(self):
        from voidx.tools.file_ops.edit_resolve import remap_line_range
        from voidx.tools.file_state import DiffSpan

        # edit 删除 20-30,老行号 22-28 完全落入删除区
        steps = [[DiffSpan(20, 30, -11)]]  # 11行 -> 0行
        assert remap_line_range(22, 28, steps) is None

    def test_range_split_returns_none(self):
        from voidx.tools.file_ops.edit_resolve import remap_line_range
        from voidx.tools.file_state import DiffSpan

        # edit 删除 25-26,老行号 20-30 被拆成 [20-24] 和 [27-30] 两段
        steps = [[DiffSpan(25, 26, -2)]]
        assert remap_line_range(20, 30, steps) is None

    def test_equivalence_invariant_with_coverage_remap(self, tmp_path):
        """remap_line_range 对单行的投影必须与 coverage ranges 一致"""
        import voidx.tools.file_state as fs
        from voidx.tools.file_ops.edit_resolve import remap_line_range
        from voidx.diffing import make_structured_diff

        f = tmp_path / "a.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        fs.record_read_range(ctx, f, 1, 10)

        key = str(f.resolve())
        # edit1: l2-l3 -> X (偏移 -1)
        old1 = f.read_text()
        f.write_text("l1\nX\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        fd1 = make_structured_diff("a.txt", old1, f.read_text())
        old_ranges1 = ctx.file_read_coverage[key]["ranges"]
        fs.remap_read_coverage_from_file_diff(ctx, f, fd1, old_ranges=old_ranges1)

        # edit2: l5-l6 -> Y (偏移 -1)
        old2 = f.read_text()
        f.write_text("l1\nX\nl4\nY\nl7\nl8\nl9\nl10\n")
        fd2 = make_structured_diff("a.txt", old2, f.read_text())
        old_ranges2 = ctx.file_read_coverage[key]["ranges"]
        fs.remap_read_coverage_from_file_diff(ctx, f, fd2, old_ranges=old_ranges2)

        # l7 原在第 7 行,edit1 后 -> 6,edit2 后 -> 5
        maps = fs.get_line_drift_maps(ctx, f)
        assert len(maps) == 1
        remapped = remap_line_range(7, 7, maps[0].span_steps)
        assert remapped is not None
        assert remapped == (5, 5)
        # coverage 应覆盖 remapped 后的行
        assert fs.covered_read_range(ctx, f, 5, 5) is not None
