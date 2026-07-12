"""Smoke tests for tool system — types, execution, error handling."""

import sys
from pathlib import Path


import pytest

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS, sanitize_tool_message_content
from voidx.tools.base import ToolContext, ToolResult, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReadTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
from voidx.tools.registry import ToolRegistry
import voidx.memory.store as store

class TestFileOps:
    """File operations work on real files."""

    def test_file_tool_guidance_is_exposed_to_model(self):
        from voidx.tools.file.manage import ManageTool
        from voidx.tools.file.write import WriteTool
        file_desc = ManageTool.description.lower()
        line_desc = WriteTool.description.lower()
        assert "create" in file_desc
        assert "delete" in file_desc
        assert "move" in file_desc
        assert "create empty files or directories" in file_desc
        assert "delete" in file_desc
        assert "move" in file_desc
        assert "write op=\"insert\"" in line_desc
        assert "op=\"append\"" in line_desc
        assert "op=\"write\"" in line_desc

    @pytest.mark.asyncio
    async def test_read(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "test.txt"}, ctx)
        expected = "1\tline1\n2\tline2\n3\tline3"
        assert result.output.strip() == expected
        assert result.metadata["lines"] == 3
        assert result.metadata["total_lines"] == 3

    @pytest.mark.asyncio
    async def test_read_empty_file_reports_zero_lines(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("read", {"file_path": "empty.txt"}, ctx)

        assert result.metadata["lines"] == 0
        assert result.metadata["total_lines"] == 0
        assert "Read 0 lines" in result.title

    @pytest.mark.asyncio
    async def test_read_rejects_files_with_null_bytes(self, tmp_path):
        f = tmp_path / "binary.dat"
        f.write_bytes(b"text before\0text after\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("read", {"file_path": "binary.dat"}, ctx)

        assert result.metadata.get("error") is True
        assert result.metadata.get("binary") is True
        assert "binary" in result.output.lower()
        assert file_state.covered_read_range(ctx, f, 1, 1) is None

    @pytest.mark.asyncio
    async def test_read_caps_output_by_message_budget_and_records_only_visible_lines(self, tmp_path):
        f = tmp_path / "long-read.txt"
        f.write_text("\n".join(f"line {i:04d} " + ("x" * 80) for i in range(1, 301)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        first = await r.execute_tool("read", {"file_path": "long-read.txt"}, ctx)
        next_offset = first.metadata["next_offset"]

        assert len(first.output) <= DEFAULT_TOOL_MESSAGE_MAX_CHARS
        assert first.metadata["lines"] < 300
        assert first.metadata["truncated_by_chars"] is True
        assert first.metadata["end_line"] == next_offset - 1
        assert file_state.covered_read_range(ctx, f, 1, first.metadata["end_line"]) is not None
        assert file_state.covered_read_range(ctx, f, next_offset, next_offset) is None
        assert first.next_step_hint == f"Read capped. Continue with read offset={next_offset}."

        second = await r.execute_tool("read", {"file_path": "long-read.txt", "offset": next_offset, "limit": 5}, ctx)

        assert second.metadata.get("already_read") is not True
        assert f"{next_offset}\tline {next_offset:04d}" in second.output

    @pytest.mark.asyncio
    async def test_read_overlong_single_line_does_not_record_full_line_coverage(self, tmp_path):
        f = tmp_path / "overlong-line.txt"
        f.write_text("prefix-" + ("x" * (DEFAULT_TOOL_MESSAGE_MAX_CHARS + 500)) + "\nsecond\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("read", {"file_path": "overlong-line.txt"}, ctx)

        assert len(result.output) <= DEFAULT_TOOL_MESSAGE_MAX_CHARS
        assert result.metadata["lines"] == 0
        assert result.metadata["truncated_single_line"] is True
        assert file_state.covered_read_range(ctx, f, 1, 1) is None
        assert "not marked as read" in result.output

    def test_record_mtime_uses_ns_and_size_fingerprint(self, tmp_path):
        f = tmp_path / "fingerprint.txt"
        f.write_text("one\n", newline="\n")
        ctx = ToolContext(workspace=str(tmp_path))

        file_state.record_mtime(ctx, f)
        stored = ctx.file_mtimes[str(f.resolve())]

        assert isinstance(stored, dict)
        assert "mtime_ns" in stored
        assert "size" in stored
        assert stored["size"] == 4

    def test_check_staleness_detects_size_change_even_when_mtime_ns_matches(self, tmp_path):
        f = tmp_path / "fingerprint-size.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))
        file_state.record_mtime(ctx, f)
        f.write_text("one plus more\n")
        key = str(f.resolve())
        current = file_state.file_fingerprint(f)
        ctx.file_mtimes[key] = {"mtime_ns": current.mtime_ns, "size": 4}

        stale = file_state.check_staleness(ctx, f)

        assert stale is not None
        assert "modified since last read" in stale

    @pytest.mark.asyncio
    async def test_read_fully_covered_range_returns_already_read_summary(self, tmp_path):
        f = tmp_path / "covered.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 121)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        first = await r.execute_tool("read", {"file_path": "covered.txt", "offset": 1, "limit": 100}, ctx)

        second = await r.execute_tool("read", {"file_path": "covered.txt", "offset": 50, "limit": 51}, ctx)

        assert "1\tline 1" in first.output
        assert second.metadata["already_read"] is True
        assert second.metadata["lines"] == 51
        assert second.metadata["covered_lines"] == 51
        assert "50\tline 50" in second.output
        assert "[Lines " not in second.output
        assert "were already read" not in second.output
        assert second.title == "Read 51 lines"
        assert second.summary == "51/120 lines"

    @pytest.mark.asyncio
    async def test_already_read_repeated_output_stays_within_llm_message_budget(self, tmp_path):
        f = tmp_path / "covered-long.txt"
        f.write_text(
            ("\n" * 99_999)
            + "\n".join(f"line {i:06d} " + ("x" * 80) for i in range(100_000, 100_200))
            + "\n"
        )
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        first = await r.execute_tool("read", {"file_path": "covered-long.txt", "offset": 100_000}, ctx)
        assert first.metadata["truncated_by_chars"] is True

        second = await r.execute_tool(
            "read",
            {"file_path": "covered-long.txt", "offset": 100_000, "limit": first.metadata["lines"]},
            ctx,
        )

        assert second.metadata["already_read"] is True
        assert len(second.output) <= DEFAULT_TOOL_MESSAGE_MAX_CHARS
        sanitized = sanitize_tool_message_content(second.output, workspace=str(tmp_path))
        assert "[Tool output truncated" not in sanitized

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_file_create_and_line_insert(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("manage", {"op": "create", "paths": "out.txt"}, ctx)
        result = await r.execute_tool(
            "write",
            {"file_path": "out.txt", "op": "insert", "lineno": 1, "new_string": "hello"},
            ctx,
        )
        assert result.metadata.get("error") is not True
        assert (tmp_path / "out.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_file_create_overwrite_and_line_insert(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        (tmp_path / "out.txt").write_text("old")
        await r.execute_tool("read", {"file_path": "out.txt"}, ctx)
        await r.execute_tool("manage", {"op": "create", "paths": "out.txt", "overwrite": True}, ctx)
        result = await r.execute_tool(
            "write",
            {"file_path": "out.txt", "op": "insert", "lineno": 1, "new_string": "new"},
            ctx,
        )
        assert result.metadata.get("error") is not True
        assert (tmp_path / "out.txt").read_text() == "new"

    @pytest.mark.asyncio
    async def test_line_insert_line_count_matches_read_display_lines(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        exactly_200_with_final_newline = "\n".join(f"line {i}" for i in range(200)) + "\n"
        await r.execute_tool("manage", {"op": "create", "paths": "exactly-200.txt"}, ctx)
        await r.execute_tool(
            "write",
            {"file_path": "exactly-200.txt", "op": "insert", "lineno": 1, "new_string": exactly_200_with_final_newline},
            ctx,
        )
        assert (tmp_path / "exactly-200.txt").read_text() == exactly_200_with_final_newline

    @pytest.mark.asyncio
    async def test_replace(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world\nkeep\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "edit.txt"}, ctx)
        result = await r.execute_tool(
            "replace",
            {"file_path": "edit.txt", "bounds": [{"line_no": 1, "anchor": "hello world"}], "new_string": "hi world"},
            ctx,
        )
        assert "File edited" in result.output
        assert (tmp_path / "edit.txt").read_text() == "hi world\nkeep\n"

    @pytest.mark.asyncio
    async def test_replace_output_contains_diff(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "edit.txt"}, ctx)
        result = await r.execute_tool(
            "replace",
            {"file_path": "edit.txt", "bounds": [{"line_no": 1, "anchor": "hello world"}], "new_string": "hi world"},
            ctx,
        )
        assert "File edited" in result.output
        assert result.diff is not None
        assert "-hello world" in result.diff
        assert "+hi world" in result.diff

    @pytest.mark.asyncio
    async def test_replace_line_range_out_of_bounds(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "short.txt"}, ctx)
        result = await r.execute_tool(
            "replace",
            {"file_path": "short.txt", "bounds": [{"line_no": 2, "anchor": "two"}], "new_string": "two"},
            ctx,
        )
        assert "not found" in result.output
        assert result.metadata.get("error")
        assert (tmp_path / "short.txt").read_text() == "one\n"

    @pytest.mark.asyncio
    async def test_replace_requires_read_coverage(self, tmp_path):
        f = tmp_path / "unread.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "replace",
            {"file_path": "unread.txt", "bounds": [{"line_no": 1, "anchor": "one"}], "new_string": "two"},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "unread.txt").read_text() == "one\n"

    @pytest.mark.asyncio
    async def test_line_insert_at_bof_and_eof(self, tmp_path):
        f = tmp_path / "insert.txt"
        f.write_text("middle\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert.txt"}, ctx)

        await r.execute_tool(
            "write",
            {"file_path": "insert.txt", "op": "insert", "lineno": 1, "new_string": "top\n"},
            ctx,
        )
        await r.execute_tool("read", {"file_path": "insert.txt"}, ctx)
        result = await r.execute_tool(
            "write",
            {"file_path": "insert.txt", "op": "append", "new_string": "bottom\n"},
            ctx,
        )

        await r.execute_tool("read", {"file_path": "insert.txt"}, ctx)

        assert result.metadata.get("error") is not True
        assert (tmp_path / "insert.txt").read_text() == "top\nmiddle\nend\nbottom\n"


class TestReadExternalPath:
    """read tool should ask for permission when reading outside workspace."""

    @pytest.mark.asyncio
    async def test_external_path_allowed_by_user(self, tmp_path):

        external = tmp_path / "external"
        external.mkdir()
        target = external / "file.txt"
        target.write_text("hello\n")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        seen_request: UserInteraction | None = None

        async def fake_interact(req: UserInteraction) -> UserResponse:
            nonlocal seen_request
            seen_request = req
            return UserResponse(value="allow")

        ctx = ToolContext(
            workspace=str(workspace),
            interact=fake_interact,
        )
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": str(target)}, ctx)

        assert result.metadata.get("error") is not True
        assert "hello" in result.output
        assert seen_request is not None
        assert seen_request.prompt == f"Read file outside workspace? {target}"
        assert seen_request.options == [
            ("Yes", "allow", "Allow this read once"),
            ("No", "deny", "Do not read this file"),
        ]

    @pytest.mark.asyncio
    async def test_external_path_denied_by_user(self, tmp_path):

        external = tmp_path / "external"
        external.mkdir()
        target = external / "file.txt"
        target.write_text("secret\n")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        async def fake_interact(req: UserInteraction) -> UserResponse:
            return UserResponse(value="deny")

        ctx = ToolContext(
            workspace=str(workspace),
            interact=fake_interact,
        )
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": str(target)}, ctx)

        assert result.metadata.get("error") is True
        assert "denied" in result.output.lower()

    @pytest.mark.asyncio
    async def test_external_path_no_interact_fallback_blocked(self, tmp_path):
        external = tmp_path / "external"
        external.mkdir()
        target = external / "file.txt"
        target.write_text("secret\n")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        ctx = ToolContext(workspace=str(workspace))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": str(target)}, ctx)

        assert result.metadata.get("error") is True
        assert "blocked" in result.output.lower()

    @pytest.mark.asyncio
    async def test_external_nonexistent_path_still_blocked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        async def fake_interact(req):
            return UserResponse(value="allow")

        ctx = ToolContext(workspace=str(workspace), interact=fake_interact)
        r = ToolRegistry()
        result = await r.execute_tool(
            "read", {"file_path": str(tmp_path / "nonexistent" / "file.txt")}, ctx
        )

        assert result.metadata.get("error") is True
        assert "not found" in result.output.lower()
