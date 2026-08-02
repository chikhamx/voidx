"""Tests for scripts/archive.py — spec document archiver."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "archive.py"


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _make_specs_tree(tmp_path: Path) -> Path:
    """Create a minimal project root with docs/specs and docs/archive."""
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "archive").mkdir(parents=True)
    return tmp_path


def test_archives_design_document(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    design_dir = root / "docs" / "design"
    design_dir.mkdir(parents=True)
    design = design_dir / "design.md"
    design.write_text("# Design\n\nBody.\n")

    result = _run([str(design)], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "design.md"
    assert archived.exists()
    assert not design.exists()
    assert "> **Status: Done**" in archived.read_text()

    assert "source: docs/design/design.md" in result.stdout
    assert "target: docs/archive/design.md" in result.stdout
    assert "status: Done" in result.stdout
    assert "dry_run: false" in result.stdout

def test_archive_defaults_status_note_to_archive_date(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "dated.md"
    spec.write_text("# Dated\n\nBody.\n")

    result = _run([str(spec), "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "dated.md"
    assert "> **Status: Done** — Archived on 2026-07-09." in archived.read_text()



# ── Status header injection ──────────────────────────────────────────


def test_no_status_adds_after_title(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "foo.md"
    spec.write_text("# Foo Design\n\nBody text.\n")

    result = _run([str(spec), "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "foo.md"
    assert archived.exists()
    assert not spec.exists()
    lines = archived.read_text().splitlines()
    assert lines[0] == "# Foo Design"
    assert lines[1] == ""
    assert lines[2] == "> **Status: Done** — Archived on 2026-07-09."


def test_no_status_no_title_prepends(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "bar.md"
    spec.write_text("Some intro without heading.\n\nMore.\n")

    result = _run([str(spec), "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "bar.md"
    lines = archived.read_text().splitlines()
    assert lines[0] == "> **Status: Done** — Archived on 2026-07-09."


def test_status_spec_replaced_with_done(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "baz.md"
    spec.write_text("# Baz\n\n> **Status: Spec** — waiting.\n\nBody.\n")

    result = _run([str(spec)], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "baz.md"
    text = archived.read_text()
    assert "> **Status: Done**" in text
    assert "Spec" not in text


def test_status_done_gets_default_archive_date(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "qux.md"
    spec.write_text("# Qux\n\n> **Status: Done** — all good.\n\nBody.\n")

    result = _run([str(spec), "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "qux.md"
    text = archived.read_text()
    assert "> **Status: Done** — Archived on 2026-07-09." in text
    assert "all good" not in text


def test_status_note_replaces_supplement(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "note.md"
    spec.write_text("# Note\n\n> **Status: Spec** — old note.\n\nBody.\n")

    result = _run([str(spec), "--status-note", "implemented and tested"], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "note.md"
    text = archived.read_text()
    assert "> **Status: Done** — implemented and tested" in text
    assert "old note" not in text


# ── Error handling ───────────────────────────────────────────────────


def test_file_not_found(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    result = _run([str(root / "docs" / "specs" / "missing.md")], cwd=root)
    assert result.returncode == 1
    assert "file not found" in result.stderr.lower()


def test_not_a_spec_file(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    other = root / "other.md"
    other.write_text("# Not in specs\n")

    result = _run([str(other)], cwd=root)
    assert result.returncode == 1
    assert "not a spec file" in result.stderr.lower()


def test_target_exists_no_overwrite(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "dup.md"
    spec.write_text("# Dup\n\nBody.\n")
    (root / "docs" / "archive" / "dup.md").write_text("# Existing\n")

    result = _run([str(spec)], cwd=root)
    assert result.returncode == 1
    assert "already exists" in result.stderr.lower()
    # original untouched
    assert (root / "docs" / "archive" / "dup.md").read_text() == "# Existing\n"


def test_partial_failure_continues(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    good = root / "docs" / "specs" / "good.md"
    good.write_text("# Good\n\nBody.\n")
    bad = root / "docs" / "specs" / "bad.md"  # doesn't exist

    result = _run([str(bad), str(good)], cwd=root)
    assert result.returncode == 1
    assert (root / "docs" / "archive" / "good.md").exists()


# ── Dry run ──────────────────────────────────────────────────────────


def test_dry_run_no_changes(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "dry.md"
    original = "# Dry\n\nBody.\n"
    spec.write_text(original)

    result = _run([str(spec), "--dry-run"], cwd=root)
    assert result.returncode == 0
    assert "dry-run" in result.stdout.lower() or "dry run" in result.stdout.lower()
    # file untouched
    assert spec.read_text() == original
    assert not (root / "docs" / "archive" / "dry.md").exists()


# ── Batch ────────────────────────────────────────────────────────────


def test_batch_archive(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    files = []
    for i in range(3):
        f = root / "docs" / "specs" / f"batch{i}.md"
        f.write_text(f"# Batch {i}\n\nBody.\n")
        files.append(f)

    result = _run([str(f) for f in files], cwd=root)
    assert result.returncode == 0, result.stderr
    for i in range(3):
        assert (root / "docs" / "archive" / f"batch{i}.md").exists()


# ── Organize archive ──────────────────────────────────────────────────


def test_organize_archive_keeps_recent_loose_files(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    recent = archive_dir / "recent.md"
    recent.write_text("# Recent\n\n> **Status: Done** — Verified on 2026-07-05.\n")
    stale = archive_dir / "stale.md"
    stale.write_text("# Stale\n\n> **Status: Done** — Verified on 2026-07-01.\n")

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert recent.exists()
    assert not stale.exists()
    assert (archive_dir / "2026-07-01" / "stale.md").exists()
    assert "organized: docs/archive/stale.md → docs/archive/2026-07-01/stale.md" in result.stdout
    assert "skipped_count: 1" in result.stdout



def test_organize_archive_uses_file_mtime_when_body_has_no_date(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    stale = archive_dir / "undated.md"
    stale.write_text("# Undated\n\n> **Status: Done**\n")
    timestamp = dt.datetime(2026, 6, 28, 12, tzinfo=dt.UTC).timestamp()
    os.utime(stale, (timestamp, timestamp))

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert not stale.exists()
    assert (archive_dir / "2026-06-28" / stale.name).exists()
    assert "organized: docs/archive/undated.md → docs/archive/2026-06-28/undated.md" in result.stdout


def test_organize_archive_ignores_filename_date_when_file_mtime_is_recent(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    recent = archive_dir / "filename-only-2026-06-28.md"
    recent.write_text("# Filename Only\n\n> **Status: Done**\n")
    timestamp = dt.datetime(2026, 7, 5, 12, tzinfo=dt.UTC).timestamp()
    os.utime(recent, (timestamp, timestamp))

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert recent.exists()
    assert not (archive_dir / "2026-06-28" / recent.name).exists()
    assert "skipped_count: 1" in result.stdout

def test_organize_archive_keeps_recent_day_dirs_and_moves_older_day_dirs(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    recent_day = archive_dir / "2026-06-28"
    old_day = archive_dir / "2026-06-24"
    recent_day.mkdir()
    old_day.mkdir()
    (recent_day / "recent.md").write_text("# Recent\n")
    (old_day / "old.md").write_text("# Old\n")

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert (recent_day / "recent.md").exists()
    assert not old_day.exists()
    assert (archive_dir / "2026-06" / "2026-06-24" / "old.md").exists()
    assert "organized: docs/archive/2026-06-24/old.md → docs/archive/2026-06/2026-06-24/old.md" in result.stdout


def test_organize_archive_keeps_recent_month_dirs_and_moves_older_month_dirs_to_year(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    recent_month = archive_dir / "2026-05" / "2026-05-10"
    old_month = archive_dir / "2026-03" / "2026-03-10"
    recent_month.mkdir(parents=True)
    old_month.mkdir(parents=True)
    (recent_month / "recent.md").write_text("# Recent\n")
    (old_month / "old.md").write_text("# Old\n")

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert (recent_month / "recent.md").exists()
    assert not (archive_dir / "2026-03").exists()
    assert (archive_dir / "2026" / "2026-03" / "2026-03-10" / "old.md").exists()
    assert "organized: docs/archive/2026-03/2026-03-10/old.md → docs/archive/2026/2026-03/2026-03-10/old.md" in result.stdout


def test_organize_archive_does_not_merge_year_dirs(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    year_day = archive_dir / "2025" / "2025-01" / "2025-01-01"
    year_day.mkdir(parents=True)
    (year_day / "done.md").write_text("# Done\n")

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert (year_day / "done.md").exists()
    assert "organized_count: 0" in result.stdout


def test_organize_archive_dry_run_leaves_files_in_place(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    old_file = archive_dir / "old.md"
    old_file.write_text("# Old\n\n> **Status: Done** — Verified on 2026-07-01.\n")

    result = _run(["docs/archive", "--today", "2026-07-09", "--dry-run"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert old_file.exists()
    assert not (archive_dir / "2026-07-01" / "old.md").exists()
    assert "would organize: docs/archive/old.md → docs/archive/2026-07-01/old.md" in result.stdout
    assert "organized_count: 1" in result.stdout
    assert "dry_run: true" in result.stdout


def test_organize_archive_dry_run_reports_conflicts_and_continues(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    conflict = archive_dir / "conflict.md"
    conflict.write_text("# Conflict\n\n> **Status: Done** — Verified on 2026-07-01.\n")
    movable = archive_dir / "movable.md"
    movable.write_text("# Movable\n\n> **Status: Done** — Verified on 2026-07-01.\n")
    target_dir = archive_dir / "2026-07-01"
    target_dir.mkdir()
    (target_dir / "conflict.md").write_text("# Existing\n")

    result = _run(["docs/archive", "--today", "2026-07-09", "--dry-run"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert conflict.exists()
    assert movable.exists()
    assert "conflict: docs/archive/conflict.md → docs/archive/2026-07-01/conflict.md" in result.stdout
    assert "would organize: docs/archive/movable.md → docs/archive/2026-07-01/movable.md" in result.stdout
    assert "organized_count: 1" in result.stdout
    assert "conflict_count: 1" in result.stdout
    assert "dry_run: true" in result.stdout


def test_organize_archive_reports_conflicts_and_continues_when_executing(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    archive_dir = root / "docs" / "archive"
    conflict = archive_dir / "conflict.md"
    conflict.write_text("# Conflict\n\n> **Status: Done** — Verified on 2026-07-01.\n")
    movable = archive_dir / "movable.md"
    movable.write_text("# Movable\n\n> **Status: Done** — Verified on 2026-07-01.\n")
    target_dir = archive_dir / "2026-07-01"
    target_dir.mkdir()
    existing = target_dir / "conflict.md"
    existing.write_text("# Existing\n")

    result = _run(["docs/archive", "--today", "2026-07-09"], cwd=root)
    assert result.returncode == 0, result.stderr

    assert conflict.exists()
    assert existing.read_text() == "# Existing\n"
    assert not movable.exists()
    assert (target_dir / "movable.md").exists()
    assert "conflict: docs/archive/conflict.md → docs/archive/2026-07-01/conflict.md" in result.stdout
    assert "organized: docs/archive/movable.md → docs/archive/2026-07-01/movable.md" in result.stdout
    assert "organized_count: 1" in result.stdout
    assert "conflict_count: 1" in result.stdout
    assert "dry_run: false" in result.stdout
