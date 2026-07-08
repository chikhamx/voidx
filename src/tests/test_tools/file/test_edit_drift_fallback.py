"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file.replace import FileReplaceTool
from voidx.tools.file.replace_resolve import _find_text_segment
from voidx.tools.registry import ToolRegistry
import voidx.tools.file.state as file_state


class TestDriftFallback:
    def _make_lines(self):
        # 10 行,edit 后 l2-l6 被替换成 X,文件变成 6 行
        return ["l1", "X", "l7", "l8", "l9", "l10"]

    def _make_map(self, epoch=1):
        from voidx.tools.file.state import LineDriftMap, ReadLineRange, DiffSpan
        # read epoch 记录的是原始 1-10;edit 20-30 -> 5行 的等价:这里用 2-6 -> 1行 (偏移 -4)
        return LineDriftMap(
            epoch=epoch,
            source_ranges=[ReadLineRange(1, 10)],
            span_steps=[[DiffSpan(2, 6, -4)]],
        )

    def test_first_match_succeeds_no_fallback(self):
        from voidx.tools.file.replace import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        # 用当前文件行号直接匹配成功
        result = _find_text_segment_with_drift_fallback(
            lines, 2, 2, "X", "X", [self._make_map()]
        )
        assert result.match is not None
        assert result.matched_map is None
        assert result.remapped_range is None

    def test_fallback_remaps_and_matches(self):
        from voidx.tools.file.replace import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        # LLM 用老行号 7-7 (实际在当前文件第 3 行),anchor "l7"
        # 首次在 ±3 搜索 7-7:lines[6..9] 不存在或不是 l7 -> 失败
        # 回退:remap 7 -> 3,重试匹配 l7 -> 成功
        result = _find_text_segment_with_drift_fallback(
            lines, 7, 7, "l7", "l7", [self._make_map()]
        )
        assert result.match is not None
        assert result.matched_map is not None
        assert result.remapped_range == (3, 3)

    def test_fallback_remap_to_wrong_content_fails(self):
        from voidx.tools.file.replace import _find_text_segment_with_drift_fallback
        from voidx.tools.file.state import LineDriftMap, ReadLineRange, DiffSpan

        # 文件 10 行,edit 把 2-6 删成 1 行,LLM 用老行号 9 找 "target"
        # remap 9 -> 4,但第 4 行是 "l8" 不是 "target",±3 内也没有
        lines = ["l1", "X", "l7", "l8", "l9", "l10"]
        bad_map = LineDriftMap(
            epoch=1,
            source_ranges=[ReadLineRange(1, 10)],
            span_steps=[[DiffSpan(2, 6, -5)]],
        )
        result = _find_text_segment_with_drift_fallback(
            lines, 9, 9, "target", "target", [bad_map]
        )
        assert result.match is None
        assert result.error is not None

    def test_multiple_candidates_same_range_equivalent(self):
        from voidx.tools.file.replace import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        # 两个 map 都 remap 到 (3,3),都匹配 l7 -> 等价命中
        maps = [self._make_map(epoch=1), self._make_map(epoch=2)]
        result = _find_text_segment_with_drift_fallback(
            lines, 7, 7, "l7", "l7", maps
        )
        assert result.match is not None

    def test_multiple_candidates_different_range_ambiguity(self):
        from voidx.tools.file.replace import _find_text_segment_with_drift_fallback
        from voidx.tools.file.state import LineDriftMap, ReadLineRange, DiffSpan

        # 20 行文件,第 9 行和第 17 行都是 "dup",相隔 8 行 (> 2*radius)
        lines = [f"l{i}" for i in range(1, 21)]
        lines[8] = "dup"   # 第 9 行
        lines[16] = "dup"  # 第 17 行
        # LLM 用老行号 5,首次在 5±3 (2-8) 搜索 -> 无 dup -> 失败
        # map1: DiffSpan(1,1,0) 无偏移,remap 5 -> 5,但 5±3 (2-8) 无 dup -> 跳过
        # 改用:map1 remap 5 -> 9 (offset +4),map2 remap 5 -> 17 (offset +12)
        # 但 5 必须在 span 之后。用 DiffSpan(1,2,-1): remap 5 -> 4? 不行。
        # 直接用 span 不覆盖 5: DiffSpan(1,1,4) -> remap 5 -> 9
        # DiffSpan(1,1,12) -> remap 5 -> 17
        map1 = LineDriftMap(
            epoch=1, source_ranges=[ReadLineRange(1, 20)],
            span_steps=[[DiffSpan(1, 1, 4)]],
        )
        map2 = LineDriftMap(
            epoch=2, source_ranges=[ReadLineRange(1, 20)],
            span_steps=[[DiffSpan(1, 1, 12)]],
        )
        result = _find_text_segment_with_drift_fallback(
            lines, 5, 5, "dup", "dup", [map1, map2]
        )
        assert result.match is None
        assert "ambig" in result.error.lower()

    def test_no_maps_returns_first_error(self):
        from voidx.tools.file.replace import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        result = _find_text_segment_with_drift_fallback(
            lines, 7, 7, "l7", "l7", []
        )
        assert result.match is None
        assert result.error is not None
