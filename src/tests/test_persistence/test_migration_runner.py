from __future__ import annotations

import sqlite3

import pytest

from voidx.persistence.migrations import MigrationPlan, MigrationRunner, MigrationStep


def test_failed_migration_step_rolls_back_data_and_user_version() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE preserved (value TEXT NOT NULL)")
    connection.execute("INSERT INTO preserved VALUES ('before')")
    connection.commit()

    def fail_mid_step(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO preserved VALUES ('during')")
        conn.execute("CREATE TABLE partial (id INTEGER)")
        raise RuntimeError("injected migration failure")

    plan = MigrationPlan(
        target_version=1,
        bootstrap_schema=(),
        steps=(MigrationStep(1, "failing", fail_mid_step),),
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        MigrationRunner().migrate(connection, plan)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute("SELECT value FROM preserved").fetchall() == [("before",)]
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='partial'"
    ).fetchone() is None
