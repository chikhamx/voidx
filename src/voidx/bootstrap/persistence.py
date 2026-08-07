"""Explicit persistence migration composition."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from voidx.persistence.migrations import MigrationPlan, MigrationRunner
from voidx.persistence.sqlite import (
    MIGRATIONS,
    SCHEMA_VERSION,
    bootstrap_schema,
    canonicalize_core_schema,
    cleanup_legacy_payload_schema,
)


def migration_plan() -> MigrationPlan:
    return MigrationPlan(
        target_version=SCHEMA_VERSION,
        bootstrap_schema=(bootstrap_schema,),
        steps=MIGRATIONS,
        cleanup=(cleanup_legacy_payload_schema, canonicalize_core_schema),
    )


def migrate_connection(connection: sqlite3.Connection) -> None:
    MigrationRunner().migrate(connection, migration_plan())
    connection.commit()


def initialize_persistence() -> None:
    from voidx.persistence.sqlite import initialize_shared_database

    initialize_shared_database()


def migrate_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        migrate_connection(connection)
    finally:
        connection.close()


__all__ = [
    "initialize_persistence",
    "migrate_connection",
    "migrate_database",
    "migration_plan",
]
