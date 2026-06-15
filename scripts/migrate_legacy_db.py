#!/usr/bin/env python3
"""Migrate legacy voidx.db (~/.voidx/voidx.db) to the new store layout.

Preserves only model_profiles. All other legacy data (sessions, messages,
turns, transcripts) is discarded. Run once before starting the new voidx.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".voidx"
LEGACY_DB = DATA_DIR / "voidx.db"
NEW_DB = DATA_DIR / "store" / "voidx.db"
MIGRATED_MARKER = DATA_DIR / "store" / ".legacy_migrated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    if MIGRATED_MARKER.exists():
        print("Already migrated, skipping.")
        return

    if not LEGACY_DB.exists():
        print("No legacy database found, nothing to do.")
        NEW_DB.parent.mkdir(parents=True, exist_ok=True)
        MIGRATED_MARKER.touch()
        return

    # Extract model_profiles from legacy db
    profiles: list[dict] = []
    try:
        conn = sqlite3.connect(str(LEGACY_DB))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT name, provider, model, api_key, base_url, protocol, created_at, updated_at "
                "FROM model_profiles"
            ).fetchall()
            profiles = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    except Exception as exc:
        print(f"Warning: could not read legacy db: {exc}", file=sys.stderr)

    # Remove legacy db and WAL files
    for path in (LEGACY_DB, LEGACY_DB.with_suffix(".db-wal"), LEGACY_DB.with_suffix(".db-shm")):
        if path.exists():
            try:
                path.unlink()
            except Exception as exc:
                print(f"Warning: could not remove {path}: {exc}", file=sys.stderr)

    # Ensure new db exists and restore profiles
    NEW_DB.parent.mkdir(parents=True, exist_ok=True)
    if not NEW_DB.exists():
        print("New database not found. Start voidx once first, then re-run this script.")
        return

    if profiles:
        conn = sqlite3.connect(str(NEW_DB))
        try:
            for p in profiles:
                conn.execute(
                    """INSERT INTO model_profiles
                           (name, provider, model, api_key, base_url, protocol, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(name) DO NOTHING""",
                    (p["name"], p["provider"], p["model"], p.get("api_key", ""),
                     p.get("base_url"), p.get("protocol"),
                     p.get("created_at", _now()), p.get("updated_at", _now())),
                )
            conn.commit()
            print(f"Restored {len(profiles)} model profile(s).")
        except Exception as exc:
            print(f"Warning: could not restore profiles: {exc}", file=sys.stderr)
        finally:
            conn.close()
    else:
        print("No model profiles found in legacy database.")

    MIGRATED_MARKER.touch()
    print("Legacy database removed. Migration complete.")


if __name__ == "__main__":
    main()
