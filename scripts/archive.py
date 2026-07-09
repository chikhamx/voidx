#!/usr/bin/env python3
"""Archive completed documents from docs/specs/ or docs/design/ to docs/archive/.

Adds or replaces the ``> **Status: Done**`` header marker, then moves the
file. Implementation verification is left to the caller (LLM).

Single-file mode:
    ``./scripts/archive.py <files...>`` archives each spec/design file to
    ``docs/archive/<filename>``. If ``--status-note`` is omitted, the marker
    defaults to ``Archived on YYYY-MM-DD.`` (using ``--today``) so the archive
    date is embedded in the body for later organization.

Organize mode:
    ``./scripts/archive.py docs/archive`` reorganizes the archive tree by
    retention rules (relative to ``--today``):
      - Loose top-level files older than 7 days → ``docs/archive/YYYY-MM-DD/``
      - Day directories older than 14 days → ``docs/archive/YYYY-MM/YYYY-MM-DD/``
      - Month directories older than 3 months → ``docs/archive/YYYY/YYYY-MM/YYYY-MM-DD/``
      - Year directories are left untouched.

Usage:
    ./scripts/archive.py <files...> [--dry-run] [--status-note TEXT] [--today YYYY-MM-DD]
    ./scripts/archive.py docs/archive [--dry-run] [--today YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
SOURCE_DIRS = {
    "specs": ROOT / "docs" / "specs",
    "design": ROOT / "docs" / "design",
}
ARCHIVE_DIR = ROOT / "docs" / "archive"

STATUS_RE = re.compile(r"^>\s*\*\*Status:\s+(\w+)\*\*(.*)$", re.MULTILINE)
DAILY_ARCHIVE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTHLY_ARCHIVE_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


def _archive_source(file_path: Path) -> tuple[str, Path] | None:
    resolved = file_path.resolve()
    for source_name, source_dir in SOURCE_DIRS.items():
        try:
            return source_name, resolved.relative_to(source_dir)
        except ValueError:
            continue
    return None


def update_status_header(text: str, status_note: str | None) -> str:
    """Add or replace the Status header in ``text``.

    - No status line: insert ``> **Status: Done**`` after the first ``#`` title
      line (or prepend if no title).
    - Existing status: replace the status word with ``Done``; replace the
      supplement with ``status_note`` if provided, otherwise keep the original.
    - Already ``Done`` with no ``status_note``: leave unchanged.
    """
    match = STATUS_RE.search(text)
    if match:
        existing_word = match.group(1)
        existing_supplement = match.group(2)
        if existing_word == "Done" and status_note is None:
            return text
        new_supplement = f" — {status_note}" if status_note else existing_supplement
        old_line = match.group(0)
        new_line = f"> **Status: Done**{new_supplement}"
        return text.replace(old_line, new_line, 1)

    status_line = "> **Status: Done**"
    if status_note:
        status_line += f" — {status_note}"
    lines = text.splitlines(keepends=True)
    if lines and lines[0].lstrip().startswith("#"):
        insert_idx = 1
        while insert_idx < len(lines) and lines[insert_idx].strip() == "":
            insert_idx += 1
        return lines[0] + "\n" + status_line + "\n\n" + "".join(lines[insert_idx:])
    return status_line + "\n\n" + text


def archive_one(
    file_path: Path,
    *,
    dry_run: bool,
    status_note: str | None,
    today: dt.date,
) -> bool:
    """Archive a single spec or design file. Returns True on success."""
    if not file_path.is_file():
        print(f"❌ file not found: {file_path}", file=sys.stderr)
        return False

    source = _archive_source(file_path)
    if source is None:
        expected = ", ".join(str(path) for path in SOURCE_DIRS.values())
        print(
            f"❌ not a spec file or design file: {file_path} (expected in one of: {expected})",
            file=sys.stderr,
        )
        return False

    source_name, rel = source
    dest = ARCHIVE_DIR / rel.name
    if dest.exists():
        print(f"❌ target already exists: {dest}", file=sys.stderr)
        return False

    source_display = Path("docs") / source_name / rel
    target_display = Path("docs") / "archive" / rel.name
    effective_status_note = status_note or f"Archived on {today.isoformat()}."

    if dry_run:
        print(f"🔍 [dry-run] would archive: {source_name}/{rel} → archive/{rel.name}")
        print(f"source: {source_display}")
        print(f"target: {target_display}")
        print("status: Done")
        print("dry_run: true")
        return True

    text = file_path.read_text()
    updated = update_status_header(text, effective_status_note)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(updated)
    file_path.unlink()
    print(f"✅ archived: {source_name}/{rel} → archive/{rel.name}")
    print(f"source: {source_display}")
    print(f"target: {target_display}")
    print("status: Done")
    print("dry_run: false")
    return True



def _relative_archive_display(path: Path) -> Path:
    return Path("docs") / "archive" / path.resolve().relative_to(ARCHIVE_DIR)


def _archive_date(file_path: Path) -> dt.date | None:
    text = file_path.read_text()
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match is not None:
        try:
            return dt.date.fromisoformat(match.group(1))
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(file_path.stat().st_mtime).date()


def _months_between(start: dt.date, end: dt.date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def _day_dir_for(archive_date: dt.date) -> Path:
    return ARCHIVE_DIR / archive_date.isoformat()


def _month_dir_for(archive_date: dt.date) -> Path:
    return ARCHIVE_DIR / archive_date.isoformat()[:7] / archive_date.isoformat()


def _year_dir_for(archive_date: dt.date) -> Path:
    return ARCHIVE_DIR / archive_date.isoformat()[:4] / archive_date.isoformat()[:7] / archive_date.isoformat()


def _organize_archive_file(file_path: Path, dest: Path, *, dry_run: bool) -> tuple[str, bool]:
    source_display = _relative_archive_display(file_path)
    target_display = _relative_archive_display(dest)
    if dest.exists():
        print(f"conflict: {source_display} → {target_display}")
        return "conflict", False

    if dry_run:
        print(f"would organize: {source_display} → {target_display}")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        file_path.rename(dest)
        print(f"organized: {source_display} → {target_display}")
    return "organized", True


def _remove_empty_dir(dir_path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        dir_path.rmdir()
    except OSError:
        return


def organize_archive(*, today: dt.date, dry_run: bool) -> bool:
    if not ARCHIVE_DIR.is_dir():
        print(f"❌ archive directory not found: {ARCHIVE_DIR}", file=sys.stderr)
        return False

    organized_count = 0
    skipped_count = 0
    conflict_count = 0

    for dir_path in sorted(ARCHIVE_DIR.iterdir()):
        if not dir_path.is_dir() or not MONTHLY_ARCHIVE_DIR_RE.match(dir_path.name):
            continue
        archive_date = dt.date.fromisoformat(f"{dir_path.name}-01")
        if _months_between(archive_date, today) <= 3:
            continue
        for day_dir in sorted(dir_path.iterdir()):
            if not day_dir.is_dir() or not DAILY_ARCHIVE_DIR_RE.match(day_dir.name):
                continue
            day = dt.date.fromisoformat(day_dir.name)
            for file_path in sorted(day_dir.iterdir()):
                if not file_path.is_file():
                    continue
                result, counted = _organize_archive_file(
                    file_path,
                    _year_dir_for(day) / file_path.name,
                    dry_run=dry_run,
                )
                if result == "conflict":
                    conflict_count += 1
                elif counted:
                    organized_count += 1
            _remove_empty_dir(day_dir, dry_run=dry_run)
        _remove_empty_dir(dir_path, dry_run=dry_run)

    for dir_path in sorted(ARCHIVE_DIR.iterdir()):
        if not dir_path.is_dir() or not DAILY_ARCHIVE_DIR_RE.match(dir_path.name):
            continue
        archive_date = dt.date.fromisoformat(dir_path.name)
        if (today - archive_date).days <= 14:
            continue
        for file_path in sorted(dir_path.iterdir()):
            if not file_path.is_file():
                continue
            result, counted = _organize_archive_file(
                file_path,
                _month_dir_for(archive_date) / file_path.name,
                dry_run=dry_run,
            )
            if result == "conflict":
                conflict_count += 1
            elif counted:
                organized_count += 1
        _remove_empty_dir(dir_path, dry_run=dry_run)

    for file_path in sorted(ARCHIVE_DIR.iterdir()):
        if not file_path.is_file():
            continue
        archive_date = _archive_date(file_path)
        if archive_date is None or (today - archive_date).days <= 7:
            skipped_count += 1
            continue

        result, counted = _organize_archive_file(
            file_path,
            _day_dir_for(archive_date) / file_path.name,
            dry_run=dry_run,
        )
        if result == "conflict":
            conflict_count += 1
        elif counted:
            organized_count += 1

    print(f"organized_count: {organized_count}")
    print(f"skipped_count: {skipped_count}")
    print(f"conflict_count: {conflict_count}")
    print(f"dry_run: {str(dry_run).lower()}")
    return True

def main() -> int:
    parser = argparse.ArgumentParser(description="Archive spec/design documents to docs/archive/.")
    parser.add_argument("files", nargs="+", help="Spec/design files to archive, or docs/archive to organize.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without moving or modifying files.")
    parser.add_argument("--status-note", default=None, help="Supplement text after 'Status: Done'.")
    parser.add_argument(
        "--today",
        default=dt.date.today().isoformat(),
        help="Reference date for organizing archive files (YYYY-MM-DD). Defaults to today.",
    )
    args = parser.parse_args()

    try:
        today = dt.date.fromisoformat(args.today)
    except ValueError:
        print(f"❌ invalid --today date: {args.today} (expected YYYY-MM-DD)", file=sys.stderr)
        return 1

    if len(args.files) == 1 and Path(args.files[0]).resolve() == ARCHIVE_DIR.resolve():
        return 0 if organize_archive(today=today, dry_run=args.dry_run) else 1

    ok = True
    for f in args.files:
        if not archive_one(Path(f), dry_run=args.dry_run, status_note=args.status_note, today=today):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
