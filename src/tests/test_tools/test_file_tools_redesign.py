import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file import FileReadTool, ManageTool, WriteTool
from voidx.tools.registry import ToolRegistry


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path), session_id="sid-file-redesign")


class TestManageToolRegistryAndSchema:
    def test_registry_exposes_manage(self):
        registry = ToolRegistry()

        assert "manage" in registry.ids()
        assert registry.get("manage") is not None
        assert "file" not in registry.ids()

    def test_manage_schema_uses_paths_and_moves(self):
        schema = ManageTool().parameters_schema()

        assert set(schema["properties"]) == {"op", "kind", "paths", "moves", "overwrite"}
        assert schema["properties"]["kind"]["default"] == "file"
        assert "directory" in ManageTool.description.lower()
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
    async def test_manage_create_accepts_legacy_file_path_arg(self, tmp_path):
        result = await ManageTool().execute({"op": "create", "file_path": "legacy.py"}, _ctx(tmp_path))

        assert result.metadata["operation"] == "create"
        assert result.metadata["succeeded"] == 1
        assert (tmp_path / "legacy.py").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_manage_create_accepts_legacy_path_arg(self, tmp_path):
        result = await ManageTool().execute({"op": "create", "path": "legacy-path.py"}, _ctx(tmp_path))

        assert result.metadata["operation"] == "create"
        assert result.metadata["succeeded"] == 1
        assert (tmp_path / "legacy-path.py").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_manage_delete_accepts_legacy_file_path_arg(self, tmp_path):
        (tmp_path / "delete-me.py").write_text("x\n", encoding="utf-8")

        result = await ManageTool().execute({"op": "delete", "file_path": "delete-me.py"}, _ctx(tmp_path))

        assert result.metadata["operation"] == "delete"
        assert result.metadata["succeeded"] == 1
        assert not (tmp_path / "delete-me.py").exists()

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
    async def test_manage_create_overwrite_on_unread_file_returns_error(self, tmp_path):
        (tmp_path / "existing.py").write_text("important\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "create", "paths": "existing.py", "overwrite": True},
            _ctx(tmp_path),
        )

        assert result.metadata["operation"] == "create"
        assert result.metadata["succeeded"] == 0
        assert result.metadata["failed"] == 1
        assert result.metadata["results"][0]["status"] == "error"
        assert "read" in result.metadata["results"][0]["reason"].lower()
        assert (tmp_path / "existing.py").read_text(encoding="utf-8") == "important\n"

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
    async def test_manage_move_accepts_legacy_file_path_and_dest_path_args(self, tmp_path):
        (tmp_path / "old-legacy.py").write_text("old\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "old-legacy.py"}, ctx)

        result = await ManageTool().execute(
            {"op": "move", "file_path": "old-legacy.py", "dest_path": "new-legacy.py"},
            ctx,
        )

        assert result.metadata["operation"] == "move"
        assert result.metadata["succeeded"] == 1
        assert not (tmp_path / "old-legacy.py").exists()
        assert (tmp_path / "new-legacy.py").read_text(encoding="utf-8") == "old\n"



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
        assert "append" in result.next_step_hint

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


class TestManageToolDirectoryOps:
    @pytest.mark.asyncio
    async def test_manage_create_directory_is_idempotent_without_write_hint(self, tmp_path):
        ctx = _ctx(tmp_path)

        created = await ManageTool().execute(
            {"op": "create", "kind": "dir", "paths": "pkg/components"}, ctx,
        )
        repeated = await ManageTool().execute(
            {"op": "create", "kind": "dir", "paths": "pkg/components"}, ctx,
        )

        assert created.metadata["results"][0]["status"] == "created"
        assert created.next_step_hint == ""
        assert repeated.metadata["results"][0]["status"] == "skipped"
        assert (tmp_path / "pkg" / "components").is_dir()

    @pytest.mark.asyncio
    async def test_manage_delete_directory_rejects_workspace_root(self, tmp_path):
        marker = tmp_path / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")

        result = await ManageTool().execute(
            {"op": "delete", "kind": "dir", "paths": "."}, _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "root" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_move_directory_rejects_workspace_root(self, tmp_path):
        marker = tmp_path / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": ".", "dest": "moved-workspace"}],
            },
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "root" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_move_directory_rejects_destination_inside_source(self, tmp_path):
        source = tmp_path / "pkg"
        source.mkdir()
        (source / "app.py").write_text("content\n", encoding="utf-8")

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": "pkg", "dest": "pkg/nested"}],
            },
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "inside" in result.metadata["results"][0]["reason"].lower()
        assert (source / "app.py").read_text(encoding="utf-8") == "content\n"

    @pytest.mark.asyncio
    async def test_manage_delete_directory_clears_only_descendant_tracking(self, tmp_path):
        child = tmp_path / "foo" / "app.py"
        sibling = tmp_path / "foobar" / "keep.py"
        child.parent.mkdir()
        sibling.parent.mkdir()
        child.write_text("child\n", encoding="utf-8")
        sibling.write_text("sibling\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "foo/app.py"}, ctx)
        await FileReadTool().execute({"file_path": "foobar/keep.py"}, ctx)
        child_key = str(child.resolve())
        sibling_key = str(sibling.resolve())

        result = await ManageTool().execute(
            {"op": "delete", "kind": "dir", "paths": "foo"}, ctx,
        )

        assert result.metadata["results"][0]["status"] == "deleted"
        assert child_key not in ctx.file_mtimes
        assert child_key not in ctx.file_read_coverage
        assert sibling_key in ctx.file_mtimes
        assert sibling_key in ctx.file_read_coverage

    @pytest.mark.asyncio
    async def test_manage_move_directory_clears_source_and_destination_tracking(self, tmp_path):
        source_child = tmp_path / "source" / "app.py"
        dest_child = tmp_path / "dest" / "old.py"
        source_child.parent.mkdir()
        dest_child.parent.mkdir()
        source_child.write_text("source\n", encoding="utf-8")
        dest_child.write_text("dest\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "source/app.py"}, ctx)
        await FileReadTool().execute({"file_path": "dest/old.py"}, ctx)
        source_key = str(source_child.resolve())
        old_dest_key = str(dest_child.resolve())

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": "source", "dest": "dest", "overwrite": True}],
            },
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "moved"
        assert source_key not in ctx.file_mtimes
        assert source_key not in ctx.file_read_coverage
        assert old_dest_key not in ctx.file_mtimes
        assert old_dest_key not in ctx.file_read_coverage
        assert (tmp_path / "dest" / "app.py").read_text(encoding="utf-8") == "source\n"

    @pytest.mark.asyncio
    async def test_manage_delete_directory_rejects_symlink(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        link = tmp_path / "linked-dir"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        result = await ManageTool().execute(
            {"op": "delete", "kind": "dir", "paths": "linked-dir"}, _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "symbolic link" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_batch_labels_preserve_file_compatibility(self, tmp_path):
        file_result = await ManageTool().execute(
            {"op": "create", "paths": "app.py"}, _ctx(tmp_path),
        )
        dir_result = await ManageTool().execute(
            {"op": "create", "kind": "dir", "paths": "pkg"}, _ctx(tmp_path),
        )

        assert file_result.title == "Created 1/1 files"
        assert dir_result.title == "Created 1/1 directories"

    @pytest.mark.asyncio
    async def test_manage_move_directory_rejects_destination_parent_of_source(self, tmp_path):
        source = tmp_path / "pkg" / "nested"
        source.mkdir(parents=True)
        marker = source / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": "pkg/nested", "dest": "pkg", "overwrite": True}],
            },
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "overlap" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_move_directory_rejects_workspace_root_destination(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        marker = tmp_path / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": "source", "dest": ".", "overwrite": True}],
            },
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "root" in result.metadata["results"][0]["reason"].lower()
        assert source.is_dir()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_delete_directory_rejects_extra_allowed_root(self, tmp_path):
        extra_root = tmp_path / "extra-root"
        extra_root.mkdir()
        marker = extra_root / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        ctx = ToolContext(
            workspace=str(tmp_path),
            session_id="sid-file-redesign",
            sandbox_extra_paths=[str(extra_root)],
        )

        result = await ManageTool().execute(
            {"op": "delete", "kind": "dir", "paths": str(extra_root)}, ctx,
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "root" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_move_directory_clears_tracking_when_move_fails_after_overwrite(
        self, tmp_path, monkeypatch,
    ):
        source_child = tmp_path / "source" / "app.py"
        dest_child = tmp_path / "dest" / "old.py"
        source_child.parent.mkdir()
        dest_child.parent.mkdir()
        source_child.write_text("source\n", encoding="utf-8")
        dest_child.write_text("dest\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "source/app.py"}, ctx)
        await FileReadTool().execute({"file_path": "dest/old.py"}, ctx)
        source_key = str(source_child.resolve())
        dest_key = str(dest_child.resolve())

        def fail_move(_source, _dest):
            raise OSError("simulated move failure")

        monkeypatch.setattr("voidx.tools.file.manage.shutil.move", fail_move)

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": "source", "dest": "dest", "overwrite": True}],
            },
            ctx,
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert source_key not in ctx.file_mtimes
        assert source_key not in ctx.file_read_coverage
        assert dest_key not in ctx.file_mtimes
        assert dest_key not in ctx.file_read_coverage
        assert source_child.read_text(encoding="utf-8") == "source\n"
        assert not (tmp_path / "dest").exists()

    @pytest.mark.asyncio
    async def test_manage_delete_directory_clears_tracking_after_partial_failure(
        self, tmp_path, monkeypatch,
    ):
        child = tmp_path / "target" / "app.py"
        child.parent.mkdir()
        child.write_text("content\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        await FileReadTool().execute({"file_path": "target/app.py"}, ctx)
        child_key = str(child.resolve())

        def partially_delete(path):
            (path / "app.py").unlink()
            raise OSError("simulated partial delete")

        monkeypatch.setattr("voidx.tools.file.manage.shutil.rmtree", partially_delete)

        result = await ManageTool().execute(
            {"op": "delete", "kind": "dir", "paths": "target"}, ctx,
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert child_key not in ctx.file_mtimes
        assert child_key not in ctx.file_read_coverage

    @pytest.mark.asyncio
    async def test_manage_create_directory_rejects_normalized_symlink_bypass(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "linked-dir"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        result = await ManageTool().execute(
            {"op": "create", "kind": "dir", "paths": "missing/../linked-dir/new"},
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "symbolic link" in result.metadata["results"][0]["reason"].lower()
        assert not (target / "new").exists()

    @pytest.mark.asyncio
    async def test_manage_delete_directory_rejects_normalized_symlink_bypass(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        link = tmp_path / "linked-dir"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        result = await ManageTool().execute(
            {"op": "delete", "kind": "dir", "paths": "missing/../linked-dir"},
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "symbolic link" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_manage_move_directory_rejects_normalized_source_symlink_bypass(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        link = tmp_path / "linked-source"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [{"src": "missing/../linked-source", "dest": "moved"}],
            },
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "symbolic link" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"
        assert not (tmp_path / "moved").exists()

    @pytest.mark.asyncio
    async def test_manage_move_directory_rejects_normalized_destination_symlink_bypass(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "source.txt").write_text("source\n", encoding="utf-8")
        target = tmp_path / "target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        link = tmp_path / "linked-dest"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        result = await ManageTool().execute(
            {
                "op": "move",
                "kind": "dir",
                "moves": [
                    {
                        "src": "source",
                        "dest": "missing/../linked-dest",
                        "overwrite": True,
                    }
                ],
            },
            _ctx(tmp_path),
        )

        assert result.metadata["results"][0]["status"] == "error"
        assert "symbolic link" in result.metadata["results"][0]["reason"].lower()
        assert marker.read_text(encoding="utf-8") == "keep\n"
        assert (source / "source.txt").read_text(encoding="utf-8") == "source\n"
