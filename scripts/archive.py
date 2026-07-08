#!/usr/bin/env python3
"""Archive spec documents from docs/specs/ to docs/archive/.

Adds or replaces the ``> **Status: Done**`` header marker, then moves the
file. Implementation verification is left to the caller (LLM).

Usage:
    ./scripts/archive.py <files...> [--dry-run] [--status-note TEXT]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
SPECS_DIR = ROOT / "docs" / "specs"
ARCHIVE_DIR = ROOT / "docs" / "archive"

STATUS_RE = re.compile(r"^>\s*\*\*Status:\s+(\w+)\*\*(.*)$", re.MULTILINE)


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
) -> bool:
    """Archive a single spec file. Returns True on success."""
    if not file_path.is_file():
        print(f"❌ file not found: {file_path}", file=sys.stderr)
        return False

    try:
        rel = file_path.resolve().relative_to(SPECS_DIR)
    except ValueError:
        print(f"❌ not a spec file: {file_path} (expected in {SPECS_DIR})", file=sys.stderr)
        return False

    dest = ARCHIVE_DIR / rel.name
    if dest.exists():
        print(f"❌ target already exists: {dest}", file=sys.stderr)
        return False

    if dry_run:
        print(f"🔍 [dry-run] would archive: specs/{rel} → archive/{rel.name}")
        return True

    text = file_path.read_text()
    updated = update_status_header(text, status_note)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(updated)
    file_path.unlink()
    print(f"✅ archived: specs/{rel} → archive/{rel.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive spec documents to docs/archive/.")
    parser.add_argument("files", nargs="+", help="Spec files to archive.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without moving or modifying files.")
    parser.add_argument("--status-note", default=None, help="Supplement text after 'Status: Done'.")
    args = parser.parse_args()

    ok = True
    for f in args.files:
        if not archive_one(Path(f), dry_run=args.dry_run, status_note=args.status_note):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
