"""SQLite persistence layer — async-safe via asyncio.to_thread()."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from voidx.platform.paths import voidx_home

DATA_DIR = voidx_home()
_SQLITE_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30000
_SQLITE_LOCK_MAX_ATTEMPTS = 4
_SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.05
_SQLITE_LOCK_RETRY_MAX_DELAY_SECONDS = 0.5

_conn: sqlite3.Connection | None = None
_init_lock = threading.Lock()
_write_lock = threading.Lock()
T = TypeVar("T")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


now = now


def _get_db() -> sqlite3.Connection:
    global _conn
    with _init_lock:
        if _conn is not None:
            return _conn
        _conn = _run_with_locked_retry(_open_db)
        return _conn


def initialize_shared_database() -> sqlite3.Connection:
    return _get_db()


def _prepare_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store_dir = DATA_DIR / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir / "voidx.db"


def _open_db() -> sqlite3.Connection:
    db_path = _prepare_db_path()
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        _init_schema(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _is_database_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def _run_with_locked_retry(operation: Callable[[], T]) -> T:
    delay = _SQLITE_LOCK_RETRY_DELAY_SECONDS
    for attempt in range(1, _SQLITE_LOCK_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_database_locked(exc) or attempt >= _SQLITE_LOCK_MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _SQLITE_LOCK_RETRY_MAX_DELAY_SECONDS)
    raise RuntimeError("unreachable sqlite retry state")


SCHEMA_VERSION = 14


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    row = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in row)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _drop_column_if_exists(conn: sqlite3.Connection, table: str, column: str) -> None:
    if _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """v0 → v1: add message_count/directory/workflow_route_json, drop legacy columns."""
    _add_column_if_missing(conn, "sessions", "message_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "sessions", "directory", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(
        conn, "session_runtime_state", "workflow_route_json", "TEXT NOT NULL DEFAULT ''"
    )
    _drop_column_if_exists(conn, "session_runtime_state", "pending_approval_json")
    _drop_column_if_exists(conn, "session_runtime_state", "recent_user_texts_json")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: add the runtime profile discriminator to sessions."""
    _add_column_if_missing(conn, "sessions", "runtime_profile", "TEXT NOT NULL DEFAULT 'coding'")


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5: persist resolved agent profile snapshots on sessions."""
    _add_column_if_missing(conn, "sessions", "runtime_profile_revision", "INTEGER")
    _add_column_if_missing(conn, "sessions", "runtime_profile_content_hash", "TEXT")
    _add_column_if_missing(conn, "sessions", "runtime_profile_hash", "TEXT")
    _add_column_if_missing(conn, "sessions", "runtime_profile_source", "TEXT")
    _add_column_if_missing(conn, "sessions", "runtime_profile_snapshot", "TEXT")


def _create_goal_protocol_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goal_protocol_records (
            protocol_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            generation TEXT NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('init', 'checkpoint', 'decision')),
            attempt_number INTEGER NOT NULL,
            sequence_number INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            payload_type TEXT NOT NULL CHECK (
                payload_type IN ('GoalSpecSnapshot', 'WorkCheckpoint', 'GoalDecision')
            ),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('submitted', 'projected')),
            payload_hash TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            projected_at TEXT,
            UNIQUE (generation, sequence_number),
            UNIQUE (generation, phase, attempt_number)
        );

        CREATE INDEX IF NOT EXISTS idx_goal_protocol_generation_sequence
            ON goal_protocol_records(generation, sequence_number);
        CREATE INDEX IF NOT EXISTS idx_goal_protocol_status
            ON goal_protocol_records(generation, status);
    """)


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """v5 → v6: add the durable Goal protocol journal."""
    _create_goal_protocol_tables(conn)


def _create_goal_generation_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goal_generations (
            generation TEXT PRIMARY KEY,
            main_session_id TEXT NOT NULL,
            evaluator_session_id TEXT NOT NULL UNIQUE,
            work_session_id TEXT NOT NULL UNIQUE,
            goal_thread_id TEXT UNIQUE,
            visibility TEXT NOT NULL DEFAULT 'internal'
                CHECK (visibility = 'internal'),
            created_at TEXT NOT NULL,
            terminal_at TEXT,
            archived_at TEXT,
            FOREIGN KEY (main_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (evaluator_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (work_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_goal_generations_main
            ON goal_generations(main_session_id, created_at);
    """)




def _create_goal_recovery_lease_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goal_recovery_leases (
            generation TEXT PRIMARY KEY,
            lease_owner TEXT NOT NULL,
            lease_expires_at REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goal_recovery_leases_expiry
            ON goal_recovery_leases(lease_expires_at);
    """)


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """v6 → v7: persist opaque Goal generation/session bindings."""
    _create_goal_generation_tables(conn)


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """v7 → v8: persist cross-worker Goal recovery leases."""
    _create_goal_recovery_lease_table(conn)



def _create_guidance_inbox_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS guidance_inbox (
            guidance_id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('user', 'system', 'guard')),
            created_at TEXT NOT NULL,
            target_session_id TEXT,
            target_thread_id TEXT,
            target_run_id TEXT,
            target_phase TEXT,
            delivery_id TEXT,
            delivered_phase TEXT,
            consumed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_guidance_inbox_pending_session
            ON guidance_inbox(target_session_id, created_at, guidance_id)
            WHERE consumed_at IS NULL AND delivery_id IS NULL;
        CREATE INDEX IF NOT EXISTS idx_guidance_inbox_pending_thread
            ON guidance_inbox(target_thread_id, created_at, guidance_id)
            WHERE consumed_at IS NULL AND delivery_id IS NULL;
        CREATE INDEX IF NOT EXISTS idx_guidance_inbox_pending_run
            ON guidance_inbox(target_run_id, target_phase, created_at, guidance_id)
            WHERE consumed_at IS NULL AND delivery_id IS NULL;
    """)


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """v8 → v9: persist cross-mode Guidance inbox records."""
    _create_guidance_inbox_table(conn)


def _migrate_to_v10(conn: sqlite3.Connection) -> None:
    """v9 → v10: persist whether the submitted Guidance text was truncated."""
    _add_column_if_missing(
        conn,
        "guidance_inbox",
        "truncated",
        "INTEGER NOT NULL DEFAULT 0",
    )


def _create_goal_generation_cleanup_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goal_generation_cleanup (
            generation TEXT PRIMARY KEY,
            cleanup_epoch INTEGER NOT NULL,
            main_session_id TEXT NOT NULL,
            work_session_id TEXT NOT NULL,
            evaluator_session_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
            requested_at TEXT NOT NULL,
            completed_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        );
    """)


def _migrate_to_v11(conn: sqlite3.Connection) -> None:
    """v10 → v11: persist durable Goal generation cleanup tombstones."""
    _create_goal_generation_cleanup_table(conn)




def _create_goal_transcript_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goal_transcript_records (
            session_id TEXT NOT NULL,
            generation TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            local_sequence INTEGER NOT NULL,
            session_sequence INTEGER NOT NULL,
            fencing_token INTEGER NOT NULL,
            filename TEXT NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            payload_hash TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            PRIMARY KEY (session_id, attempt_id, local_sequence),
            UNIQUE (session_id, session_sequence),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (generation) REFERENCES goal_generations(generation) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_goal_transcript_order
            ON goal_transcript_records(session_id, session_sequence);
    """)


def _migrate_to_v12(conn: sqlite3.Connection) -> None:
    """v11 → v12: index accepted Goal transcript byte ranges."""
    _create_goal_transcript_table(conn)


def _create_goal_runtime_failure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goal_runtime_failures (
            generation TEXT PRIMARY KEY,
            observed_sequence INTEGER NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY (generation) REFERENCES goal_generations(generation)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS goal_public_summary_outbox (
            summary_id TEXT PRIMARY KEY,
            generation TEXT NOT NULL,
            main_session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            UNIQUE (generation, kind),
            FOREIGN KEY (main_session_id) REFERENCES sessions(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_goal_public_summary_pending
            ON goal_public_summary_outbox(main_session_id, delivered_at, created_at);
    """)


def _migrate_to_v13(conn: sqlite3.Connection) -> None:
    """v12 → v13: persist atomic Goal runtime failures and public summaries."""
    _create_goal_runtime_failure_tables(conn)


def _migrate_to_v14(conn: sqlite3.Connection) -> None:
    """v13 → v14: persist structured Goal public summary payloads."""
    _add_column_if_missing(
        conn,
        "goal_public_summary_outbox",
        "payload_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )


def _create_agent_thread_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_threads (
            id TEXT PRIMARY KEY,
            parent_thread_id TEXT,
            session_id TEXT,
            workspace TEXT NOT NULL DEFAULT '',
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            profile_json TEXT NOT NULL,
            resource_scope_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_thread_state (
            thread_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            state_version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS agent_thread_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS agent_thread_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            prefix_hash TEXT NOT NULL,
            frame_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS runtime_turn_attempts (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            source_outbox_id TEXT NOT NULL UNIQUE,
            input_frame_json TEXT NOT NULL,
            base_state_version INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            side_effect_started INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT NOT NULL DEFAULT '',
            fencing_token INTEGER NOT NULL,
            lease_expires_at REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS runtime_outbox (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            source_attempt_id TEXT,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            expected_state_version INTEGER NOT NULL,
            available_at REAL NOT NULL DEFAULT 0,
            claimed_by TEXT,
            claimed_until REAL,
            delivered_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE,
            FOREIGN KEY (source_attempt_id) REFERENCES runtime_turn_attempts(id) ON DELETE CASCADE,
            UNIQUE(source_attempt_id, kind)
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_outbox_ready
            ON runtime_outbox(delivered_at, available_at, claimed_until);
        CREATE INDEX IF NOT EXISTS idx_runtime_attempts_thread
            ON runtime_turn_attempts(thread_id, status);
    """)


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: add durable agent thread, attempt, and outbox tables."""
    _create_agent_thread_tables(conn)


def _create_provisional_session_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS provisional_sessions (
            session_id TEXT PRIMARY KEY,
            root_session_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_provisional_sessions_root
            ON provisional_sessions(root_session_id);
        CREATE INDEX IF NOT EXISTS idx_provisional_sessions_owner_created
            ON provisional_sessions(owner_id, created_at);
    """)


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: add provisional session lifecycle markers."""
    _create_provisional_session_table(conn)


from voidx.persistence.migrations import MigrationPlan, MigrationRunner, MigrationStep


MIGRATIONS = (
    MigrationStep(1, "sessions-runtime-columns", _migrate_to_v1),
    MigrationStep(2, "runtime-profile", _migrate_to_v2),
    MigrationStep(3, "agent-thread-tables", _migrate_to_v3),
    MigrationStep(4, "provisional-sessions", _migrate_to_v4),
    MigrationStep(5, "profile-snapshot", _migrate_to_v5),
    MigrationStep(6, "goal-protocol-journal", _migrate_to_v6),
    MigrationStep(7, "goal-generation-bindings", _migrate_to_v7),
    MigrationStep(8, "goal-recovery-leases", _migrate_to_v8),
    MigrationStep(9, "guidance-inbox", _migrate_to_v9),
    MigrationStep(10, "guidance-truncated-flag", _migrate_to_v10),
    MigrationStep(11, "goal-generation-cleanup", _migrate_to_v11),
    MigrationStep(12, "goal-transcript-records", _migrate_to_v12),
    MigrationStep(13, "goal-runtime-failures", _migrate_to_v13),
    MigrationStep(14, "goal-public-summary-payload", _migrate_to_v14),
)


def bootstrap_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New session',
            workspace TEXT NOT NULL DEFAULT '.',
            directory TEXT NOT NULL DEFAULT '',
            model_provider TEXT NOT NULL DEFAULT 'anthropic',
            model_name TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            runtime_profile TEXT NOT NULL DEFAULT 'coding',
            runtime_profile_revision INTEGER,
            runtime_profile_content_hash TEXT,
            runtime_profile_hash TEXT,
            runtime_profile_source TEXT,
            runtime_profile_snapshot TEXT
        );

        CREATE TABLE IF NOT EXISTS session_runtime_state (
            session_id TEXT PRIMARY KEY,
            interaction_mode TEXT NOT NULL DEFAULT 'auto',
            current_intent TEXT NOT NULL DEFAULT 'coding',
            previous_intent TEXT,
            current_goal_json TEXT,
            workflow_route_json TEXT NOT NULL DEFAULT '',
            workflow_runs_json TEXT NOT NULL DEFAULT '{}',
            todo_state_json TEXT NOT NULL DEFAULT '',
            compaction_summary TEXT NOT NULL DEFAULT '',
            session_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS context_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_message_id INTEGER,
            frame_kind TEXT NOT NULL DEFAULT 'main',
            agent_persona TEXT NOT NULL DEFAULT 'voidx',
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prefix_hash TEXT NOT NULL,
            frame_hash TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            file_path TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_context_frames_session
            ON context_frames(session_id, id);

        CREATE INDEX IF NOT EXISTS idx_context_frames_prefix
            ON context_frames(session_id, prefix_hash);

        CREATE TABLE IF NOT EXISTS model_profiles (
            name TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            base_url TEXT,
            protocol TEXT,
            reasoning_effort TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_model_profiles_provider
            ON model_profiles(provider);
    """)
    _create_agent_thread_tables(conn)
    _create_provisional_session_table(conn)


def _init_schema(conn: sqlite3.Connection) -> None:
    plan = MigrationPlan(
        target_version=SCHEMA_VERSION,
        bootstrap_schema=(bootstrap_schema,),
        steps=MIGRATIONS,
        cleanup=(cleanup_legacy_payload_schema, canonicalize_core_schema),
    )
    MigrationRunner().migrate(conn, plan)
    conn.commit()


def cleanup_legacy_payload_schema(conn: sqlite3.Connection) -> None:
    if not any(_table_exists(conn, table) for table in (
        "messages",
        "turns",
        "transcript_nodes",
        "message_runtime_snapshots",
    )):
        return

    conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in ("transcript_nodes", "turns", "message_runtime_snapshots", "messages"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")




def canonicalize_core_schema(conn: sqlite3.Connection) -> None:
    session_columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
    runtime_columns = [
        row[1] for row in conn.execute("PRAGMA table_info(session_runtime_state)")
    ]
    runtime_foreign_keys = conn.execute(
        "PRAGMA foreign_key_list(session_runtime_state)"
    ).fetchall()
    expected_session_columns = [
        "id",
        "title",
        "workspace",
        "directory",
        "model_provider",
        "model_name",
        "created_at",
        "updated_at",
        "message_count",
        "runtime_profile",
        "runtime_profile_revision",
        "runtime_profile_content_hash",
        "runtime_profile_hash",
        "runtime_profile_source",
        "runtime_profile_snapshot",
    ]
    expected_runtime_columns = [
        "session_id",
        "interaction_mode",
        "current_intent",
        "previous_intent",
        "current_goal_json",
        "workflow_route_json",
        "workflow_runs_json",
        "todo_state_json",
        "compaction_summary",
        "session_time",
        "updated_at",
    ]
    if (
        session_columns == expected_session_columns
        and runtime_columns == expected_runtime_columns
        and runtime_foreign_keys
    ):
        return

    session_rows = [dict(row) for row in conn.execute("SELECT * FROM sessions")]
    runtime_rows = [
        dict(row) for row in conn.execute("SELECT * FROM session_runtime_state")
    ]
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute("DROP TABLE session_runtime_state")
            conn.execute("DROP TABLE sessions")
            bootstrap_schema(conn)
            _insert_rows(conn, "sessions", expected_session_columns, session_rows)
            _insert_rows(
                conn,
                "session_runtime_state",
                expected_runtime_columns,
                runtime_rows,
            )
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    available = [column for column in columns if all(column in row for row in rows)]
    placeholders = ", ".join("?" for _ in available)
    names = ", ".join(available)
    conn.executemany(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
        [tuple(row[column] for column in available) for row in rows],
    )
def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None





async def execute_commit(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    def _run():
        conn = _get_db()
        with _write_lock:
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur
            except Exception:
                conn.rollback()
                raise
    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def fetch_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    def _run():
        conn = _get_db()
        with _write_lock:
            return conn.execute(sql, params).fetchall()
    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def fetch_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    def _run():
        conn = _get_db()
        with _write_lock:
            return conn.execute(sql, params).fetchone()
    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def write_transaction(callback: Callable[[sqlite3.Connection], T]) -> T:
    def _run() -> T:
        conn = _get_db()
        with _write_lock:
            try:
                result = callback(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


def open_isolated_db(db_path: Path) -> sqlite3.Connection:
    """Open a standalone database file with the full voidx schema."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        _init_schema(conn)
        return conn
    except Exception:
        conn.close()
        raise


async def fetch_all_on(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> list[sqlite3.Row]:
    def _run():
        with _write_lock:
            return conn.execute(sql, params).fetchall()

    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def fetch_one_on(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> sqlite3.Row | None:
    def _run():
        with _write_lock:
            return conn.execute(sql, params).fetchone()

    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def write_transaction_on(
    conn: sqlite3.Connection, callback: Callable[[sqlite3.Connection], T]
) -> T:
    def _run() -> T:
        with _write_lock:
            try:
                result = callback(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))
