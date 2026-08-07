"""Tests for file edit operations — replace and line insert via registry."""

from tests.tool_registry import build_registry
import sys
from pathlib import Path


import pytest

from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.builtin.file.replace import FileReplaceTool
from voidx.tooling.builtin.file.replace_resolve import _find_text_segment
from voidx.tooling.application.registry import ToolRegistry
import voidx.tooling.application.file_state as file_state


class TestDriftFallbackE2E:
    @pytest.mark.asyncio
    async def test_drift_fallback_e2e(self, tmp_path):
        f = tmp_path / "drift.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "drift.txt"}, ctx)

        # edit: l2-l6 (5行) -> X (1行),偏移 -4,l7 从第 7 行变成第 3 行
        await r.execute_tool(
            "replace",
            {
                "file_path": "drift.txt",
                "bounds": [{"line_no": 2, "anchor": "l2"}, {"line_no": 6, "anchor": "l6"}],
                "new_string": "X",
            },
            ctx,
        )

        # LLM 用老行号 7-7 找 "l7",首次在 7±3 搜索失败,回退 remap 7->3 匹配成功
        result = await r.execute_tool(
            "replace",
            {
                "file_path": "drift.txt",
                "bounds": [{"line_no": 7, "anchor": "l7"}],
                "new_string": "L7",
            },
            ctx,
        )
        assert result.metadata.get("error") is not True
        assert "drift fallback" in result.output.lower()
        assert f.read_text() == "l1\nX\nL7\nl8\nl9\nl10\n"

    @pytest.mark.asyncio
    async def test_drift_fallback_accumulates_multiple_edits(self, tmp_path):
        """两次 edit 后用最初 read 的老行号走 fallback,验证 step 序列累积正确。

        read 1-10
        edit1: l2-l4 -> X (3行->1行,偏移 -2),l10 从第 10 行 -> 第 8 行
        edit2: l5-l7 -> Y (3行->1行,偏移 -2),l10 从第 8 行 -> 第 6 行
        LLM 用老行号 10-10 找 "l10":首次 10±3=7-13 搜索,第 6 行不在范围 -> 失败
        回退: remap 10 -> 8 (edit1) -> 6 (edit2),重试匹配 l10 -> 成功
        """
        f = tmp_path / "drift_multi.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "drift_multi.txt"}, ctx)

        # edit1: l2-l4 -> X (偏移 -2)
        await r.execute_tool(
            "replace",
            {
                "file_path": "drift_multi.txt",
                "bounds": [{"line_no": 2, "anchor": "l2"}, {"line_no": 4, "anchor": "l4"}],
                "new_string": "X",
            },
            ctx,
        )
        # edit2: l5-l7 -> Y (edit1 后 l5/l6/l7 仍在第 5-7 行,偏移 -2)
        await r.execute_tool(
            "replace",
            {
                "file_path": "drift_multi.txt",
                "bounds": [{"line_no": 5, "anchor": "l5"}, {"line_no": 7, "anchor": "l7"}],
                "new_string": "Y",
            },
            ctx,
        )

        # 当前文件: l1\nX\nY\nl8\nl9\nl10  -> l10 在第 6 行
        # LLM 用老行号 10-10 找 "l10",首次 10±3=7-13 搜索失败(文件只有 6 行)
        # 回退: remap 10 -> 8 (edit1) -> 6 (edit2),重试匹配 l10 -> 成功
        result = await r.execute_tool(
            "replace",
            {
                "file_path": "drift_multi.txt",
                "bounds": [{"line_no": 10, "anchor": "l10"}],
                "new_string": "L10",
            },
            ctx,
        )
        assert result.metadata.get("error") is not True
        assert "drift fallback" in result.output.lower()
        assert "epoch #1" in result.output
        assert f.read_text() == "l1\nX\nY\nl8\nl9\nL10\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_leading_newline(self, tmp_path):
        """start_anchor with leading \\n should be normalized to its first non-empty line."""
        f = tmp_path / "lead-nl.txt"
        f.write_text("line1\ndef foo():\n    return 1\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "lead-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "lead-nl.txt",
                "bounds": [{"line_no": 2, "anchor": "\ndef foo():"}],
                "new_string": "def bar():",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\ndef bar():\n    return 1\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_trailing_newline(self, tmp_path):
        """start_anchor with trailing \\n should be normalized to its first non-empty line."""
        f = tmp_path / "trail-nl.txt"
        f.write_text("line1\ndef foo():\n    return 1\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "trail-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "trail-nl.txt",
                "bounds": [{"line_no": 2, "anchor": "def foo():\n"}],
                "new_string": "def bar():",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\ndef bar():\n    return 1\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_middle_newline(self, tmp_path):
        """start_anchor with \\n in the middle should be normalized to its first non-empty line."""
        f = tmp_path / "mid-nl.txt"
        f.write_text("line1\ndef foo():\n    return 1\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "mid-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "mid-nl.txt",
                "bounds": [{"line_no": 2, "anchor": "def foo():\n    return 1"}, {"line_no": 3, "anchor": "    return 1\ndef foo():"}],
                "new_string": "def bar():\n    return 2",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\ndef bar():\n    return 2\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_pure_newline_matches_empty_line(self, tmp_path):
        """anchor of pure \\n should be normalized to empty string and match an empty line."""
        f = tmp_path / "pure-nl.txt"
        f.write_text("line1\n\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "pure-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "pure-nl.txt",
                "bounds": [{"line_no": 2, "anchor": "\n"}],
                "new_string": "INSERTED",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nINSERTED\nline3\n"
