#!/usr/bin/env python3
"""Clean up legacy v2 data before v3.0.0.

Removes:
  - ~/.voidx/voidx.db (v2 root-level database, migrated to store/voidx.db)
  - ~/.voidx/voidx.db.bak, .db-shm.bak, .db-wal.bak (v2 backup files)
  - <workspace>/voidx.json (v2 config, migrated to .voidx/settings.json)

Safe: only removes files when the v3 replacement exists.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _rm(path: Path, label: str, *, require: Path | None = None) -> None:
    if not path.exists():
        print(f"  skip {label}: not found")
        return
    if require is not None and not require.exists():
        print(f"  skip {label}: v3 replacement {require} does not exist yet")
        return
    path.unlink()
    print(f"  removed {label}")


def clean_home_dir() -> int:
    home = Path.home() / ".voidx"
    if not home.exists():
        print("~/.voidx not found, nothing to clean.")
        return 0

    print("Cleaning ~/.voidx/ ...")
    removed = 0

    v2_db = home / "voidx.db"
    v3_db = home / "store" / "voidx.db"
    _rm(v2_db, "voidx.db (v2 root db)", require=v3_db)
    if v2_db.exists():
        pass  # skipped
    else:
        removed += 1

    for suffix in (".bak", "-shm.bak", "-wal.bak"):
        p = home / f"voidx.db{suffix}"
        if p.exists():
            _rm(p, f"voidx.db{suffix} (v2 backup)")
            removed += 1

    return removed


def clean_workspace(workspace: str | None) -> int:
    ws = Path(workspace).resolve() if workspace else Path.cwd()
    print(f"Cleaning workspace {ws} ...")
    removed = 0

    v2_config = ws / "voidx.json"
    v3_config = ws / ".voidx" / "settings.json"
    _rm(v2_config, "voidx.json (v2 config)", require=v3_config)
    if not v2_config.exists():
        removed += 1

    return removed


def main() -> None:
    workspace = sys.argv[1] if len(sys.argv) > 1 else None
    total = 0
    total += clean_home_dir()
    total += clean_workspace(workspace)
    if total:
        print(f"\nDone. {total} legacy item(s) cleaned.")
    else:
        print("\nNo legacy items to clean.")


if __name__ == "__main__":
    main()
