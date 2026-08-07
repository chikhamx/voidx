"""Explicit SQLite migration plan and runner."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass


MigrationCallable = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationStep:
    version: int
    name: str
    apply: MigrationCallable


@dataclass(frozen=True)
class MigrationPlan:
    target_version: int
    bootstrap_schema: tuple[MigrationCallable, ...]
    steps: tuple[MigrationStep, ...]
    cleanup: tuple[MigrationCallable, ...] = ()


class MigrationRunner:
    def migrate(self, connection: sqlite3.Connection, plan: MigrationPlan) -> None:
        if plan.target_version < 0:
            raise ValueError("target migration version must not be negative")
        versions = [step.version for step in plan.steps]
        if versions != list(range(1, plan.target_version + 1)):
            raise ValueError("migration versions must be continuous from 1")
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for schema in plan.bootstrap_schema:
            schema(connection)
        for step in plan.steps:
            if step.version <= current:
                continue
            try:
                with connection:
                    step.apply(connection)
                    connection.execute(f"PRAGMA user_version={step.version}")
            except Exception:
                connection.execute(f"PRAGMA user_version={current}")
                raise
            current = step.version
        for cleanup in plan.cleanup:
            cleanup(connection)
