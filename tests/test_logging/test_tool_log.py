"""Tests for voidx.logging.tool_log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.logging.tool_log import log_tool_event


class TestLogToolEvent:
    def test_writes_jsonl_entry(self, tmp_path: Path) -> None:
        log_file = tmp_path / "agent_events.jsonl"
        log_tool_event(
            "hidden_tool_failure",
            tool_name="clarify",
            message="clarify failed internally",
            log_path=log_file,
        )

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "hidden_tool_failure"
        assert entry["tool_name"] == "clarify"
        assert entry["message"] == "clarify failed internally"
        assert "ts" in entry

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        log_file = tmp_path / "agent_events.jsonl"
        log_tool_event("ui_warn", message="something warned", log_path=log_file)
        log_tool_event("ui_error", message="something errored", log_path=log_file)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "ui_warn"
        assert json.loads(lines[1])["event"] == "ui_error"

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        log_file = tmp_path / "nested" / "dir" / "agent_events.jsonl"
        log_tool_event("hidden_tool_failure", tool_name="x", message="y", log_path=log_file)
        assert log_file.exists()

    def test_includes_optional_session_id(self, tmp_path: Path) -> None:
        log_file = tmp_path / "agent_events.jsonl"
        log_tool_event(
            "hidden_tool_failure",
            tool_name="plan_checkpoint",
            message="timeout",
            session_id="sess-123",
            log_path=log_file,
        )

        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["session_id"] == "sess-123"

    def test_omits_session_id_when_absent(self, tmp_path: Path) -> None:
        log_file = tmp_path / "agent_events.jsonl"
        log_tool_event("ui_warn", message="no session", log_path=log_file)

        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert "session_id" not in entry

    def test_does_not_raise_on_write_failure(self, tmp_path: Path) -> None:
        log_file = tmp_path / "readonly" / "agent_events.jsonl"
        log_file.parent.mkdir()
        log_file.touch()
        log_file.chmod(0o444)

        # Should not raise
        log_tool_event("ui_error", message="should not crash", log_path=log_file)


class TestLogRotation:
    def test_rotates_when_file_exceeds_limit(self, tmp_path: Path) -> None:
        from voidx.logging.tool_log import _MAX_LOG_BYTES, _rotate_if_needed

        log_file = tmp_path / "agent_events.jsonl"
        log_file.write_text("x" * (_MAX_LOG_BYTES + 1), encoding="utf-8")

        _rotate_if_needed(log_file)

        rotated = tmp_path / "agent_events.1.jsonl"
        assert rotated.exists()
        assert rotated.stat().st_size == _MAX_LOG_BYTES + 1
        assert log_file.stat().st_size == 0

    def test_no_rotation_when_under_limit(self, tmp_path: Path) -> None:
        from voidx.logging.tool_log import _MAX_LOG_BYTES, _rotate_if_needed

        log_file = tmp_path / "agent_events.jsonl"
        log_file.write_text("small", encoding="utf-8")

        _rotate_if_needed(log_file)

        rotated = tmp_path / "agent_events.1.jsonl"
        assert not rotated.exists()
        assert log_file.read_text(encoding="utf-8") == "small"

    def test_shifts_existing_rotated_files(self, tmp_path: Path) -> None:
        from voidx.logging.tool_log import _MAX_LOG_BYTES, _rotate_if_needed

        log_file = tmp_path / "agent_events.jsonl"
        rot1 = tmp_path / "agent_events.1.jsonl"
        rot2 = tmp_path / "agent_events.2.jsonl"

        rot1.write_text("old-1", encoding="utf-8")
        log_file.write_text("x" * (_MAX_LOG_BYTES + 1), encoding="utf-8")

        _rotate_if_needed(log_file)

        assert rot2.exists()
        assert rot2.read_text(encoding="utf-8") == "old-1"
        assert rot1.stat().st_size == _MAX_LOG_BYTES + 1
        assert log_file.stat().st_size == 0
