"""Tests for replace tool failure logging — verifies log_tool_event is called
with detailed diagnostics (request params, anchor line text, failure reason)
on every failure branch."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file_ops import FileReadTool, FileReplaceTool
import voidx.tools.file_ops.edit_execute as edit_execute


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path), session_id="sid-test")


def _calls(mock_log):
    return [c.kwargs for c in mock_log.call_args_list]


# ── anchor not found: the most common replace failure ──────────────────────

@pytest.mark.asyncio
async def test_anchor_not_found_logs_detailed_diagnostics(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    await FileReadTool().execute({"file_path": "app.py"}, ctx)

    with patch.object(edit_execute, "log_tool_event") as mock_log:
        result = await FileReplaceTool().execute(
            {
                "file_path": "app.py",
                "bounds": [{"line_no": 1, "anchor": "missing-anchor"}],
                "new_string": "replaced\n",
            },
            ctx,
        )

    assert result.metadata.get("error") is True
    assert mock_log.called, "log_tool_event should be called on replace failure"
    kw = _calls(mock_log)[0]
    assert kw["tool_name"] == "replace"
    assert "app.py" in kw["message"]
    assert "start_no" in kw["message"] and "1" in kw["message"]
    assert "start_anchor" in kw["message"] and "missing-anchor" in kw["message"]
    # actual line text from the file should be included for debugging
    assert "line one" in kw["message"]
    assert kw.get("session_id") == "sid-test"


# ── file not found ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_file_not_found_logs_failure(tmp_path):
    ctx = _ctx(tmp_path)
    with patch.object(edit_execute, "log_tool_event") as mock_log:
        result = await FileReplaceTool().execute(
            {
                "file_path": "nope.py",
                "bounds": [{"line_no": 1, "anchor": "x"}],
                "new_string": "y\n",
            },
            ctx,
        )
    assert result.metadata.get("error") is True
    assert mock_log.called
    kw = _calls(mock_log)[0]
    assert kw["tool_name"] == "replace"
    assert "nope.py" in kw["message"]
    assert "File not found" in kw["message"]


# ── coverage error: editing without reading first ──────────────────────────

@pytest.mark.asyncio
async def test_coverage_error_logs_failure(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("line one\nline two\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    # deliberately do NOT read the file first → coverage check fails

    with patch.object(edit_execute, "log_tool_event") as mock_log:
        result = await FileReplaceTool().execute(
            {
                "file_path": "app.py",
                "bounds": [{"line_no": 1, "anchor": "line one"}],
                "new_string": "replaced\n",
            },
            ctx,
        )
    assert result.metadata.get("error") is True
    assert mock_log.called
    kw = _calls(mock_log)[0]
    assert kw["tool_name"] == "replace"
    assert "app.py" in kw["message"]
    assert "read" in kw["message"].lower()


# ── invalid args: pydantic validation failure ───────────────────────────────

@pytest.mark.asyncio
async def test_invalid_args_logs_failure(tmp_path):
    ctx = _ctx(tmp_path)
    with patch.object(edit_execute, "log_tool_event") as mock_log:
        result = await FileReplaceTool().execute({"file_path": 123}, ctx)
    assert result.metadata.get("error") is True
    assert mock_log.called
    kw = _calls(mock_log)[0]
    assert kw["tool_name"] == "replace"
    assert "Invalid arguments" in kw["message"]


# ── ambiguous range: multiple candidates with same score ────────────────────

@pytest.mark.asyncio
async def test_ambiguous_range_logs_failure(tmp_path):
    # Two identical lines equidistant from target → ambiguous
    # line 1 and line 3 both contain "dup", both distance 1 from start_no=2
    target = tmp_path / "dup.py"
    target.write_text("dup\n\ndup\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    await FileReadTool().execute({"file_path": "dup.py"}, ctx)

    with patch.object(edit_execute, "log_tool_event") as mock_log:
        result = await FileReplaceTool().execute(
            {
                "file_path": "dup.py",
                "bounds": [{"line_no": 2, "anchor": "dup"}],
                "new_string": "x\n",
            },
            ctx,
        )
    assert result.metadata.get("error") is True
    assert mock_log.called
    kw = _calls(mock_log)[0]
    assert kw["tool_name"] == "replace"
    assert "ambig" in kw["message"].lower() or "ambiguous" in kw["message"].lower()
