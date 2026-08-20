from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from voidx.bootstrap.persistence import migrate_connection


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "persistence"


def schema_manifest(connection: sqlite3.Connection) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    objects = [
        dict(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]
    tables = [row["name"] for row in objects if row["type"] == "table"]
    return {
        "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "objects": objects,
        "tables": {
            table: {
                "columns": [dict(row) for row in connection.execute(f'PRAGMA table_info("{table}")')],
                "foreign_keys": [dict(row) for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')],
            }
            for table in tables
        },
    }


def expected_manifest() -> dict[str, object]:
    return json.loads((FIXTURES / "schema_v5.json").read_text(encoding="utf-8"))


def test_fresh_and_repeated_initialization_match_schema_manifest(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "fresh.db")
    migrate_connection(connection)
    assert schema_manifest(connection) == expected_manifest()
    migrate_connection(connection)
    assert schema_manifest(connection) == expected_manifest()
    connection.close()


@pytest.mark.parametrize("version", range(4))
def test_versioned_database_migrates_without_data_loss(version: int, tmp_path: Path) -> None:
    target = tmp_path / f"v{version}.db"
    shutil.copyfile(FIXTURES / f"v{version}.db", target)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    migrate_connection(connection)
    assert schema_manifest(connection) == expected_manifest()
    row = connection.execute(
        "SELECT id, title, workspace, model_provider, model_name FROM sessions WHERE id='session-fixed'"
    ).fetchone()
    assert dict(row) == {
        "id": "session-fixed",
        "title": "Preserved session",
        "workspace": "${WORKSPACE}",
        "model_provider": "anthropic",
        "model_name": "claude-sonnet-4-6",
    }
    connection.close()
