"""Tests for scripts/archive.py — spec document archiver."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
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


# ── Status header injection ──────────────────────────────────────────


def test_no_status_adds_after_title(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "foo.md"
    spec.write_text("# Foo Design\n\nBody text.\n")

    result = _run([str(spec)], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "foo.md"
    assert archived.exists()
    assert not spec.exists()
    lines = archived.read_text().splitlines()
    assert lines[0] == "# Foo Design"
    assert lines[1] == ""
    assert lines[2] == "> **Status: Done**"


def test_no_status_no_title_prepends(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "bar.md"
    spec.write_text("Some intro without heading.\n\nMore.\n")

    result = _run([str(spec)], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "bar.md"
    lines = archived.read_text().splitlines()
    assert lines[0] == "> **Status: Done**"


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


def test_status_done_unchanged(tmp_path: Path) -> None:
    root = _make_specs_tree(tmp_path)
    spec = root / "docs" / "specs" / "qux.md"
    original = "# Qux\n\n> **Status: Done** — all good.\n\nBody.\n"
    spec.write_text(original)

    result = _run([str(spec)], cwd=root)
    assert result.returncode == 0, result.stderr

    archived = root / "docs" / "archive" / "qux.md"
    assert archived.read_text() == original


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
