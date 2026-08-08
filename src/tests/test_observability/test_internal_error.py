"""Tests for voidx.observability.internal_error."""

from __future__ import annotations

import json
from pathlib import Path

from voidx.observability.internal_error import log_internal_error


class TestLogInternalError:
    def test_writes_jsonl_entry_with_exception(self, tmp_path: Path) -> None:
        log_file = tmp_path / "internal_error.jsonl"
        try:
            raise ValueError("boom")
        except ValueError as exc:
            log_internal_error(exc, context="transcript_log_write", log_path=log_file)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "internal_error"
        assert entry["context"] == "transcript_log_write"
        assert entry["error_type"] == "ValueError"
        assert entry["error_message"] == "boom"
        assert "ts" in entry
        assert "traceback" in entry

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        log_file = tmp_path / "internal_error.jsonl"
        log_internal_error(RuntimeError("first"), context="ctx_a", log_path=log_file)
        log_internal_error(RuntimeError("second"), context="ctx_b", log_path=log_file)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["error_message"] == "first"
        assert json.loads(lines[1])["error_message"] == "second"

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        log_file = tmp_path / "nested" / "dir" / "internal_error.jsonl"
        log_internal_error(RuntimeError("x"), context="y", log_path=log_file)
        assert log_file.exists()

    def test_includes_optional_session_id(self, tmp_path: Path) -> None:
        log_file = tmp_path / "internal_error.jsonl"
        log_internal_error(
            RuntimeError("x"),
            context="y",
            session_id="sess-42",
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["session_id"] == "sess-42"

    def test_omits_session_id_when_absent(self, tmp_path: Path) -> None:
        log_file = tmp_path / "internal_error.jsonl"
        log_internal_error(RuntimeError("x"), context="y", log_path=log_file)
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert "session_id" not in entry

    def test_never_raises_on_write_failure(self, tmp_path: Path, monkeypatch) -> None:
        log_file = tmp_path / "internal_error.jsonl"
        monkeypatch.setattr(
            "voidx.observability.internal_error._rotate_if_needed",
            lambda path: (_ for _ in ()).throw(OSError("rotate failed")),
        )
        log_internal_error(RuntimeError("x"), context="y", log_path=log_file)
