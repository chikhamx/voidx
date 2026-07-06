"""Tests for tool result persistence — large output saved to disk."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voidx.agent.tool_result_storage import (
    TOOL_RESULT_PERSIST_THRESHOLD,
    _make_preview,
    _persist_to_disk,
    cleanup_session_results,
    maybe_persist_tool_result,
)


class TestMakePreview:
    def test_short_content_unchanged(self):
        content = "short"
        assert _make_preview(content, 100) == content

    def test_long_content_truncated(self):
        content = "x" * 1000
        preview = _make_preview(content, 100)
        assert len(preview) <= 110  # some slack for the ellipsis line
        assert "…" in preview

    def test_preview_has_head_and_tail(self):
        content = "A" * 700 + "B" * 300
        preview = _make_preview(content, 100)
        assert preview.startswith("A")
        assert preview.endswith("B")


class TestPersistToDisk:
    def test_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        content = "test content"
        path = _persist_to_disk(content, "call_123", session_id="test-session")
        assert path is not None
        assert os.path.exists(path)
        with open(path) as f:
            assert f.read() == content

    def test_sanitizes_tool_use_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        path = _persist_to_disk("data", "call_with/special:chars", session_id="s")
        assert "/" not in Path(path).name
        assert ":" not in Path(path).name


class TestMaybePersistToolResult:
    def test_small_content_not_persisted(self):
        content = "small output"
        result = maybe_persist_tool_result(content, "call_1", "bash")
        assert result == content

    def test_large_content_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        content = "x" * (TOOL_RESULT_PERSIST_THRESHOLD + 1)
        result = maybe_persist_tool_result(content, "call_2", "bash", session_id="test")
        assert "<persisted-output>" in result
        assert "Preview:" in result
        assert "Saved to:" in result

    def test_read_tool_exempt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        content = "x" * (TOOL_RESULT_PERSIST_THRESHOLD + 1)
        result = maybe_persist_tool_result(content, "call_3", "read")
        assert result == content
        assert "<persisted-output>" not in result

    def test_custom_threshold(self):
        content = "medium output"
        result = maybe_persist_tool_result(content, "call_4", "bash", threshold=5)
        assert "<persisted-output>" in result

    def test_os_error_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        content = "x" * (TOOL_RESULT_PERSIST_THRESHOLD + 1)

        def bad_mkdir(*a, **kw):
            raise OSError("no space")

        monkeypatch.setattr(Path, "mkdir", bad_mkdir)
        result = maybe_persist_tool_result(content, "call_5", "bash", session_id="test")
        assert result == content


class TestCleanupSessionResults:
    def test_removes_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _persist_to_disk("data", "call_1", session_id="cleanup-test")
        cleanup_session_results("cleanup-test")
        dir_path = tmp_path / ".voidx" / "tool-results" / "cleanup-test"
        assert not dir_path.exists()

    def test_nonexistent_session_no_error(self):
        cleanup_session_results("nonexistent-session-xyz")
