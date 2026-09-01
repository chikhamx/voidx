from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from voidx.tooling.adapters.persistence.file_snapshot import save_file_version
from voidx.tooling.domain.context import ToolExecutionContext


def _history_rows(tmp_path, session_id: str) -> list[dict]:
    manifest = tmp_path / ".voidx" / "sessions" / session_id / "file-history" / "manifest.jsonl"
    return [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.asyncio
async def test_concurrent_save_same_path_assigns_unique_versions(tmp_path, monkeypatch):
    import voidx.tooling.adapters.persistence.file_snapshot as snapshot_module

    target = tmp_path / "hot.py"
    target.write_text("before\n", encoding="utf-8")
    ctx = ToolExecutionContext(workspace=str(tmp_path), session_id="same-path")
    monkeypatch.setattr(snapshot_module, "_snapshot_matches", lambda *_args: False)

    results = await asyncio.gather(
        *(save_file_version(ctx, target, tool_name="replace") for _ in range(32)),
        return_exceptions=True,
    )

    assert [result for result in results if isinstance(result, BaseException)] == []
    rows = _history_rows(tmp_path, "same-path")
    assert [row["version"] for row in rows] == list(range(28, 33))
    assert len({(row["full_hash"], row["version"]) for row in rows}) == 5
    history_dir = tmp_path / ".voidx" / "sessions" / "same-path" / "file-history"
    assert all((history_dir / row["snapshot"]).is_file() for row in rows)
    assert not list(history_dir.glob("*.tmp"))


@pytest.mark.asyncio
async def test_concurrent_save_different_paths_succeeds(tmp_path):
    targets = [tmp_path / f"file_{index}.py" for index in range(8)]
    for index, target in enumerate(targets):
        target.write_text(f"before {index}\n", encoding="utf-8")
    ctx = ToolExecutionContext(workspace=str(tmp_path), session_id="different-paths")

    await asyncio.gather(
        *(save_file_version(ctx, target, tool_name="write") for target in targets)
    )

    rows = _history_rows(tmp_path, "different-paths")
    assert len(rows) == len(targets)
    assert {row["resolved_path"] for row in rows} == {
        str(target.resolve()) for target in targets
    }


@pytest.mark.asyncio
async def test_cancelled_save_holds_session_lock_until_manifest_append_finishes(
    tmp_path,
    monkeypatch,
):
    import threading

    import voidx.tooling.adapters.persistence.file_snapshot as snapshot_module

    target = tmp_path / "hot.py"
    target.write_text("first\n", encoding="utf-8")
    ctx = ToolExecutionContext(workspace=str(tmp_path), session_id="cancelled-save")
    first_append_started = threading.Event()
    first_append_finished = threading.Event()
    later_append_finished = threading.Event()
    release_first_append = threading.Event()
    append_count = 0
    append_count_lock = threading.Lock()
    original_append = snapshot_module._append_manifest_row

    def controlled_append(path, row):
        nonlocal append_count
        with append_count_lock:
            append_count += 1
            call_number = append_count
        if call_number == 1:
            first_append_started.set()
            release_first_append.wait(timeout=2)
            original_append(path, row)
            first_append_finished.set()
            return
        original_append(path, row)
        later_append_finished.set()

    monkeypatch.setattr(snapshot_module, "_append_manifest_row", controlled_append)

    first = asyncio.create_task(save_file_version(ctx, target, tool_name="replace"))
    assert await asyncio.to_thread(first_append_started.wait, 1)
    first.cancel()
    target.write_text("second\n", encoding="utf-8")
    second = asyncio.create_task(save_file_version(ctx, target, tool_name="replace"))

    async def release_blocked_append():
        await asyncio.to_thread(later_append_finished.wait, 0.1)
        release_first_append.set()

    releaser = asyncio.create_task(release_blocked_append())
    results = await asyncio.gather(first, second, releaser, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1:] == [None, None]
    assert await asyncio.to_thread(first_append_finished.wait, 1)

    rows = _history_rows(tmp_path, "cancelled-save")
    assert sorted(row["version"] for row in rows) == [1, 2]
    assert len({(row["full_hash"], row["version"]) for row in rows}) == 2
    history_dir = tmp_path / ".voidx" / "sessions" / "cancelled-save" / "file-history"
    assert all((history_dir / row["snapshot"]).is_file() for row in rows)


@pytest.mark.asyncio
async def test_save_same_path_keeps_only_latest_five_versions(tmp_path):
    target = tmp_path / "hot.py"
    target.write_text("before 0\n", encoding="utf-8")
    ctx = ToolExecutionContext(workspace=str(tmp_path), session_id="retention")

    for index in range(7):
        await save_file_version(ctx, target, tool_name="replace")
        target.write_text(f"before {index + 1}\n", encoding="utf-8")

    rows = _history_rows(tmp_path, "retention")
    assert [row["version"] for row in rows] == [3, 4, 5, 6, 7]
    history_dir = tmp_path / ".voidx" / "sessions" / "retention" / "file-history"
    assert {path.name for path in history_dir.iterdir()} == {
        "manifest.jsonl",
        *(row["snapshot"] for row in rows),
    }
    assert [
        (history_dir / row["snapshot"]).read_text(encoding="utf-8")
        for row in rows
    ] == [f"before {index}\n" for index in range(2, 7)]


@pytest.mark.asyncio
async def test_save_same_content_again_does_not_append_version(tmp_path):
    target = tmp_path / "same.py"
    target.write_text("same\n", encoding="utf-8")
    ctx = ToolExecutionContext(workspace=str(tmp_path), session_id="dedupe")

    await save_file_version(ctx, target, tool_name="replace")
    await save_file_version(ctx, target, tool_name="replace")

    rows = _history_rows(tmp_path, "dedupe")
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    history_dir = tmp_path / ".voidx" / "sessions" / "dedupe" / "file-history"
    assert (history_dir / rows[0]["snapshot"]).is_file()
    assert len([path for path in history_dir.iterdir() if path.name != "manifest.jsonl"]) == 1


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_save_prunes_old_versions_for_other_files_in_manifest(tmp_path):
    target = tmp_path / "target.py"
    other = tmp_path / "other.py"
    target.write_text("target\n", encoding="utf-8")
    other.write_text("other\n", encoding="utf-8")
    ctx = ToolExecutionContext(workspace=str(tmp_path), session_id="all-files-retention")
    history_dir = tmp_path / ".voidx" / "sessions" / "all-files-retention" / "file-history"
    history_dir.mkdir(parents=True)

    other_full_hash = hashlib.sha256(str(other.resolve()).encode("utf-8")).hexdigest()
    old_rows = []
    for version in range(1, 7):
        snapshot = f"{other_full_hash[:16]}@v{version}"
        (history_dir / snapshot).write_text(f"other {version}\n", encoding="utf-8")
        old_rows.append({
            "created_at": f"2026-01-01T00:00:0{version}+00:00",
            "path": "other.py",
            "resolved_path": str(other.resolve()),
            "full_hash": other_full_hash,
            "short_hash": other_full_hash[:16],
            "version": version,
            "snapshot": snapshot,
            "tool": "replace",
        })
    (history_dir / "manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in old_rows),
        encoding="utf-8",
    )

    await save_file_version(ctx, target, tool_name="write")

    rows = _history_rows(tmp_path, "all-files-retention")
    other_rows = [row for row in rows if row["full_hash"] == other_full_hash]
    assert [row["version"] for row in other_rows] == [2, 3, 4, 5, 6]
    assert not (history_dir / f"{other_full_hash[:16]}@v1").exists()
    assert all((history_dir / row["snapshot"]).is_file() for row in rows)
    assert {
        path.name for path in history_dir.iterdir()
    } == {"manifest.jsonl", *(row["snapshot"] for row in rows)}
