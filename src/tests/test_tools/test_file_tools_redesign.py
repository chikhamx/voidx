import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file import FileReadTool, FileTool, ManageTool, WriteTool
from voidx.tools.registry import ToolRegistry


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path), session_id="sid-file-redesign")


class TestManageToolRegistryAndSchema:
    def test_registry_exposes_manage_and_keeps_legacy_file_alias(self):
        registry = ToolRegistry()

        assert "manage" in registry.ids()
        assert "file" in registry.ids()
        assert registry.get("manage") is not None
        assert registry.get("file") is not None

    def test_manage_schema_uses_paths_and_moves(self):
        schema = ManageTool().parameters_schema()

        assert set(schema["properties"]) == {"op", "paths", "moves", "overwrite"}
        assert "file_path" not in schema["properties"]
        assert "dest_path" not in schema["properties"]
        assert "create" in schema["properties"]["op"]["description"]
        assert "move" in schema["properties"]["moves"]["description"]


class TestManageToolExecution:
    @pytest.mark.asyncio
    async def test_manage_create_accepts_single_path_string(self, tmp_path):
        result = await ManageTool().execute({"op": "create", "paths": "pkg/app.py"}, _ctx(tmp_path))

        assert result.metadata["operation"] == "create"
        assert result.metadata["total"] == 1
        assert result.metadata["succeeded"] == 1
        assert result.metadata["results"] == [{"file": "pkg/app.py", "status": "created"}]
        assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_manage_create_batch_reports_partial_success(self, tmp_path):
        (tmp_path / "exists.py").write_text("keep\n", encoding="utf-8")
        ctx = _ctx(tmp_path)

        result = await ManageTool().execute(
            {"op": "create", "paths": ["new.py", "exists.py"]},
            ctx,
        )

        assert result.metadata["operation"] == "create"
        assert result.metadata["total"] == 2
        assert result.metadata["succeeded"] == 1
        assert result.metadata["skipped"] == 1
        assert result.metadata["failed"] == 0
        assert result.metadata["results"][0] == {"file": "new.py", "status": "created"}
        assert result.metadata["results"][1]["file"] == "exists.py"
        assert result.metadata["results"][1]["status"] == "skipped"
        assert (tmp_path / "exists.py").read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_create_overwrite_on_unread_file_succeeds(self, tmp_path):
        (tmp_path / "existing.py").write_text("important\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "create", "paths": "existing.py", "overwrite": True},
            _ctx(tmp_path),
        )

        assert result.metadata["operation"] == "create"
        assert result.metadata["succeeded"] == 1
        assert result.metadata["failed"] == 0
        assert result.metadata["results"][0]["status"] == "created"
        assert (tmp_path / "existing.py").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_manage_move_uses_per_item_overwrite_and_keeps_order(self, tmp_path):
        (tmp_path / "old.py").write_text("old\n", encoding="utf-8")
        (tmp_path / "dest.py").write_text("dest\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "old.py"}, ctx)
        await FileReadTool().execute({"file_path": "dest.py"}, ctx)

        result = await ManageTool().execute(
            {
                "op": "move",
                "moves": [
                    {"src": "old.py", "dest": "dest.py", "overwrite": True},
                    {"src": "missing.py", "dest": "unused.py"},
                ],
            },
            ctx,
        )

        assert result.metadata["operation"] == "move"
        assert result.metadata["total"] == 2
        assert result.metadata["succeeded"] == 1
        assert result.metadata["skipped"] == 1
        assert result.metadata["failed"] == 0
        assert result.metadata["results"][0] == {"file": "old.py", "dest": "dest.py", "status": "moved"}
        assert result.metadata["results"][1]["file"] == "missing.py"
        assert result.metadata["results"][1]["status"] == "skipped"
        assert not (tmp_path / "old.py").exists()
        assert (tmp_path / "dest.py").read_text(encoding="utf-8") == "old\n"

    @pytest.mark.asyncio
    async def test_legacy_file_wrapper_maps_old_schema_to_manage(self, tmp_path):
        result = await ToolRegistry().get("file").execute(
            {"op": "move", "file_path": "old.py", "dest_path": "new.py"},
            _ctx(tmp_path),
        )

        assert result.metadata["operation"] == "move"
        assert result.metadata["results"][0]["status"] == "skipped"
        assert result.metadata["deprecated_tool"] == "file"
        assert result.metadata["replacement_tool"] == "manage"


class TestWriteToolRedesign:
    @pytest.mark.asyncio
    async def test_write_insert_lineno_is_one_based(self, tmp_path):
        (tmp_path / "app.py").write_text("one\ntwo\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)

        result = await WriteTool().execute(
            {"file_path": "app.py", "op": "insert", "lineno": 1, "new_string": "zero\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "zero\none\ntwo\n"

    @pytest.mark.asyncio
    async def test_write_insert_lineno_zero_reports_migration_hint(self, tmp_path):
        (tmp_path / "app.py").write_text("one\n", encoding="utf-8")

        result = await WriteTool().execute(
            {"file_path": "app.py", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            _ctx(tmp_path),
        )

        assert result.metadata.get("error") is True
        assert "1-based" in result.output
        assert "lineno=1" in result.output

    @pytest.mark.asyncio
    async def test_write_op_write_creates_missing_file_with_parent_dirs(self, tmp_path):
        result = await WriteTool().execute(
            {"file_path": "pkg/app.py", "op": "write", "new_string": "print('hi')\n"},
            _ctx(tmp_path),
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["operation"] == "write"
        assert result.metadata["created"] is True
        assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"

    @pytest.mark.asyncio
    async def test_write_op_write_requires_fresh_read_before_overwriting(self, tmp_path):
        (tmp_path / "app.py").write_text("old\n", encoding="utf-8")

        result = await WriteTool().execute(
            {"file_path": "app.py", "op": "write", "new_string": "new\n"},
            _ctx(tmp_path),
        )

        assert result.metadata.get("error") is True
        assert "read" in result.output.lower()
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old\n"

class TestManageAndWriteGaps:
    """Testing gaps identified in code review — manage + write edge cases."""

    # ── manage.delete ──

    @pytest.mark.asyncio
    async def test_manage_delete_staleness_mismatch_returns_error(self, tmp_path):
        (tmp_path / "app.py").write_text("content\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)
        (tmp_path / "app.py").write_text("modified\n", encoding="utf-8")

        result = await ManageTool().execute({"op": "delete", "paths": "app.py"}, ctx)

        assert result.metadata["results"][0]["status"] == "error"
        assert "modified" in result.metadata["results"][0]["reason"].lower()
        assert (tmp_path / "app.py").exists()

    @pytest.mark.asyncio
    async def test_manage_delete_nonexistent_returns_skipped(self, tmp_path):
        result = await ManageTool().execute(
            {"op": "delete", "paths": "ghost.py"}, _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "skipped"
        assert "does not exist" in result.metadata["results"][0]["reason"]

    @pytest.mark.asyncio
    async def test_manage_delete_directory_returns_error(self, tmp_path):
        (tmp_path / "adir").mkdir()

        result = await ManageTool().execute(
            {"op": "delete", "paths": "adir"}, _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "directory" in result.metadata["results"][0]["reason"].lower()

    # ── manage.create overwrite + staleness ──

    @pytest.mark.asyncio
    async def test_manage_create_overwrite_staleness_mismatch_returns_error(self, tmp_path):
        (tmp_path / "app.py").write_text("original\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)
        (tmp_path / "app.py").write_text("modified\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "create", "paths": "app.py", "overwrite": True},
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "modified" in result.metadata["results"][0]["reason"].lower()
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "modified\n"

    @pytest.mark.asyncio
    async def test_manage_create_overwrite_staleness_match_succeeds(self, tmp_path):
        (tmp_path / "app.py").write_text("original\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)

        result = await ManageTool().execute(
            {"op": "create", "paths": "app.py", "overwrite": True},
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "created"
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_manage_create_path_traversal_returns_error(self, tmp_path):
        result = await ManageTool().execute(
            {"op": "create", "paths": "../escape.py"}, _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "traversal" in result.metadata["results"][0]["reason"].lower()

    @pytest.mark.asyncio
    async def test_manage_create_path_is_directory_returns_error(self, tmp_path):
        (tmp_path / "adir").mkdir()

        result = await ManageTool().execute(
            {"op": "create", "paths": "adir"}, _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "directory" in result.metadata["results"][0]["reason"].lower()

    # ── manage.move staleness ──

    @pytest.mark.asyncio
    async def test_manage_move_src_staleness_mismatch_returns_error(self, tmp_path):
        (tmp_path / "old.py").write_text("old\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "old.py"}, ctx)
        (tmp_path / "old.py").write_text("modified\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "move", "moves": [{"src": "old.py", "dest": "new.py"}]},
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "modified" in result.metadata["results"][0]["reason"].lower()

    @pytest.mark.asyncio
    async def test_manage_move_dest_staleness_mismatch_returns_error(self, tmp_path):
        (tmp_path / "old.py").write_text("old\n", encoding="utf-8")
        (tmp_path / "dest.py").write_text("dest\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "old.py"}, ctx)
        await FileReadTool().execute({"file_path": "dest.py"}, ctx)
        (tmp_path / "dest.py").write_text("modified\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "move", "moves": [{"src": "old.py", "dest": "dest.py", "overwrite": True}]},
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "modified" in result.metadata["results"][0]["reason"].lower()

    @pytest.mark.asyncio
    async def test_manage_move_partial_failure_keeps_first_success(self, tmp_path):
        (tmp_path / "keep.py").write_text("keep\n", encoding="utf-8")
        (tmp_path / "fail.py").write_text("fail\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "keep.py"}, ctx)
        await FileReadTool().execute({"file_path": "fail.py"}, ctx)
        (tmp_path / "fail.py").write_text("modified\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "move", "moves": [
                {"src": "keep.py", "dest": "keep_moved.py"},
                {"src": "fail.py", "dest": "fail_moved.py"},
            ]},
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "moved"
        assert result.metadata["results"][1]["status"] == "error"
        assert not (tmp_path / "keep.py").exists()
        assert (tmp_path / "keep_moved.py").exists()
        assert (tmp_path / "fail.py").read_text(encoding="utf-8") == "modified\n"
        assert not (tmp_path / "fail_moved.py").exists()

    @pytest.mark.asyncio
    async def test_manage_move_dest_path_traversal_returns_error(self, tmp_path):
        (tmp_path / "safe.py").write_text("safe\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "move", "moves": [{"src": "safe.py", "dest": "../escape.py"}]},
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "traversal" in result.metadata["results"][0]["reason"].lower()

    @pytest.mark.asyncio
    async def test_manage_move_src_path_traversal_returns_error(self, tmp_path):
        result = await ManageTool().execute(
            {"op": "move", "moves": [{"src": "../escape.py", "dest": "dest.py"}]},
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "traversal" in result.metadata["results"][0]["reason"].lower()

    # ── write.write + staleness ──

    @pytest.mark.asyncio
    async def test_write_op_write_overwrite_staleness_match_succeeds(self, tmp_path):
        (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)

        result = await WriteTool().execute(
            {"file_path": "app.py", "op": "write", "new_string": "new\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["created"] is False
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "new\n"

    # ── write.insert at total_lines + 1 ──

    @pytest.mark.asyncio
    async def test_write_insert_at_total_plus_one_succeeds(self, tmp_path):
        (tmp_path / "app.py").write_text("one\ntwo\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)

        result = await WriteTool().execute(
            {"file_path": "app.py", "op": "insert", "lineno": 3, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_write_insert_beyond_total_plus_one_rejected(self, tmp_path):
        (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "app.py"}, ctx)

        result = await WriteTool().execute(
            {"file_path": "app.py", "op": "insert", "lineno": 10, "new_string": "x\n"},
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "Cannot insert" in result.output
