"""Durable store for runtime-owned agent threads and turn attempts."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import (
    AgentThread,
    AgentThreadState,
    LifecycleState,
    RuntimeDecision,
    RuntimeOutboxItem,
    ThreadAttempt,
    apply_lifecycle_decision,
)
from voidx.memory.store import (
    _fetch_all,
    _fetch_one,
    _now,
    _write_transaction,
    fetch_all_on,
    fetch_one_on,
    open_isolated_db,
    write_transaction_on,
)


class ThreadStateConflict(RuntimeError):
    """Raised when an optimistic state_version update loses the race."""


@dataclass(frozen=True)
class LoadedThread:
    thread: AgentThread
    profile: RuntimeProfile
    state: AgentThreadState
    state_version: int
    resource_scope: dict[str, Any]


@dataclass(frozen=True)
class CommitResult:
    state_version: int
    lifecycle: str
    next_outbox_id: str | None = None


class ThreadStore:
    def __init__(self, db_path: Any = None) -> None:
        # db_path given → isolated database (tests/tools); None → shared global store.
        self._conn = open_isolated_db(db_path) if db_path is not None else None

    def _write(self, tx: Any) -> Any:
        if self._conn is not None:
            return write_transaction_on(self._conn, tx)
        return _write_transaction(tx)

    def _one(self, sql: str, params: tuple = ()) -> Any:
        if self._conn is not None:
            return fetch_one_on(self._conn, sql, params)
        return _fetch_one(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> Any:
        if self._conn is not None:
            return fetch_all_on(self._conn, sql, params)
        return _fetch_all(sql, params)
    async def create_thread(
        self,
        thread: AgentThread,
        *,
        profile: RuntimeProfile,
        state: AgentThreadState | None = None,
        resource_scope: dict[str, Any] | None = None,
    ) -> LoadedThread:
        thread_state = state or AgentThreadState(
            thread_id=thread.thread_id, lifecycle=thread.lifecycle
        )
        if thread_state.thread_id != thread.thread_id:
            raise ValueError("thread state id must match thread id")
        scope = resource_scope or {}

        def _tx(conn):
            now = _now()
            conn.execute(
                """INSERT INTO agent_threads (
                       id, parent_thread_id, session_id, workspace, profile_id,
                       profile_revision, profile_json, resource_scope_json,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    thread.thread_id,
                    thread.parent_thread_id,
                    thread.session_id,
                    thread.workspace,
                    profile.profile_id,
                    profile.revision,
                    _json_profile(profile),
                    _json(scope),
                    now,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO agent_thread_state (
                       thread_id, state_json, state_version, updated_at
                   ) VALUES (?, ?, 0, ?)""",
                (thread.thread_id, _json_model(thread_state), now),
            )
            return LoadedThread(thread, profile, thread_state, 0, scope)

        return await self._write(_tx)

    async def load(self, thread_id: str) -> LoadedThread | None:
        row = await self._one(
            """SELECT t.*, s.state_json, s.state_version
               FROM agent_threads t
               JOIN agent_thread_state s ON s.thread_id = t.id
               WHERE t.id = ?""",
            (thread_id,),
        )
        if row is None:
            return None
        profile = RuntimeProfile.model_validate_json(row["profile_json"])
        thread = AgentThread(
            thread_id=row["id"],
            session_id=row["session_id"],
            parent_thread_id=row["parent_thread_id"],
            workspace=row["workspace"],
            lifecycle=AgentThread.model_fields["lifecycle"].default,
        )
        state = AgentThreadState.model_validate_json(row["state_json"])
        return LoadedThread(
            thread=thread,
            profile=profile,
            state=state,
            state_version=int(row["state_version"]),
            resource_scope=json.loads(row["resource_scope_json"] or "{}"),
        )

    async def rebind_thread_session(self, thread_id: str, session_id: str) -> None:
        def _tx(conn):
            now = _now()
            conn.execute(
                "UPDATE agent_threads SET session_id = ?, updated_at = ? WHERE id = ?",
                (session_id, now, thread_id),
            )

        await self._write(_tx)

    async def save_state(
        self,
        thread_id: str,
        state: AgentThreadState,
        *,
        expected_state_version: int,
    ) -> LoadedThread:
        def _tx(conn):
            now = _now()
            cur = conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(state), now, thread_id, expected_state_version),
            )
            if cur.rowcount != 1:
                raise ThreadStateConflict("thread state_version conflict")
            conn.execute("UPDATE agent_threads SET updated_at = ? WHERE id = ?", (now, thread_id))
            return _loaded_from_conn(conn, thread_id)

        return await self._write(_tx)

    async def begin_attempt(
        self,
        *,
        thread_id: str,
        source_outbox_id: str,
        input_frame: dict[str, Any],
        expected_state_version: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> ThreadAttempt:
        def _tx(conn):
            existing = conn.execute(
                "SELECT * FROM runtime_turn_attempts WHERE source_outbox_id = ?",
                (source_outbox_id,),
            ).fetchone()
            if existing is not None:
                return _attempt_from_row(existing)
            loaded = _loaded_from_conn(conn, thread_id)
            if loaded.state_version != expected_state_version:
                raise ThreadStateConflict("thread state_version conflict")
            now = _now()
            attempt_id = _uid("attempt")
            fencing_token = 1
            conn.execute(
                """INSERT INTO runtime_turn_attempts (
                       id, thread_id, source_outbox_id, input_frame_json,
                       base_state_version, profile_id, profile_revision, status,
                       side_effect_started, lease_owner, fencing_token,
                       lease_expires_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', 0, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    thread_id,
                    source_outbox_id,
                    _json(input_frame),
                    expected_state_version,
                    loaded.profile.profile_id,
                    loaded.profile.revision,
                    lease_owner,
                    fencing_token,
                    time.time() + lease_seconds,
                    now,
                ),
            )
            running = loaded.state.model_copy(update={"lifecycle": LifecycleState.RUNNING})
            conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(running), now, thread_id, expected_state_version),
            )
            return ThreadAttempt(
                attempt_id=attempt_id,
                thread_id=thread_id,
                source_outbox_id=source_outbox_id,
                state_version=expected_state_version + 1,
                fencing_token=fencing_token,
                status="prepared",
            )

        return await self._write(_tx)

    async def mark_side_effect_started(self, attempt_id: str) -> ThreadAttempt:
        def _tx(conn):
            now = _now()
            conn.execute(
                """UPDATE runtime_turn_attempts
                   SET side_effect_started = 1, updated_at = ?
                   WHERE id = ?""",
                (now, attempt_id),
            )
            row = conn.execute("SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            return _attempt_from_row(row)

        return await self._write(_tx)

    async def commit_decision(
        self,
        *,
        attempt_id: str,
        decision: RuntimeDecision,
        expected_state_version: int,
    ) -> CommitResult:
        def _tx(conn):
            attempt = conn.execute(
                "SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            if attempt["status"] == "committed":
                existing = conn.execute(
                    "SELECT id FROM runtime_outbox WHERE source_attempt_id = ? AND kind = 'wakeup'",
                    (attempt_id,),
                ).fetchone()
                return CommitResult(
                    state_version=int(attempt["base_state_version"]) + 1,
                    lifecycle="committed",
                    next_outbox_id=existing["id"] if existing is not None else None,
                )
            thread_id = attempt["thread_id"]
            loaded = _loaded_from_conn(conn, thread_id)
            if loaded.state_version != expected_state_version:
                raise ThreadStateConflict("thread state_version conflict")
            next_lifecycle = apply_lifecycle_decision(loaded.state.lifecycle, decision)
            context = dict(loaded.state.context)
            try:
                iteration = int(context.get("iteration", 0) or 0)
            except (TypeError, ValueError):
                iteration = 0
            context["iteration"] = iteration + 1
            next_state = loaded.state.model_copy(
                update={
                    "lifecycle": next_lifecycle,
                    "lifecycle_decision": decision,
                    "context": context,
                }
            )
            now = _now()
            next_version = expected_state_version + 1
            conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = ?, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(next_state), next_version, now, thread_id, expected_state_version),
            )
            conn.execute(
                "UPDATE runtime_turn_attempts SET status = 'committed', updated_at = ? WHERE id = ?",
                (now, attempt_id),
            )
            outbox_id = None
            if decision.outcome == "continue":
                outbox_id = _uid("wakeup")
                prior_frame = json.loads(attempt["input_frame_json"] or "{}")
                wakeup_payload = {
                    "decision": decision.model_dump(mode="json"),
                    **{
                        key: prior_frame[key]
                        for key in ("prompt", "display_text", "spec")
                        if key in prior_frame
                    },
                }
                conn.execute(
                    """INSERT OR IGNORE INTO runtime_outbox (
                           id, thread_id, source_attempt_id, kind, payload_json,
                           expected_state_version, available_at, created_at
                       ) VALUES (?, ?, ?, 'wakeup', ?, ?, ?, ?)""",
                    (
                        outbox_id,
                        thread_id,
                        attempt_id,
                        _json(wakeup_payload),
                        next_version,
                        time.time() + float(decision.next_delay_seconds or 0),
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM runtime_outbox WHERE source_attempt_id = ? AND kind = 'wakeup'",
                    (attempt_id,),
                ).fetchone()
                outbox_id = row["id"] if row is not None else None
            return CommitResult(
                state_version=next_version,
                lifecycle=next_lifecycle.value,
                next_outbox_id=outbox_id,
            )

        return await self._write(_tx)

    async def enqueue_outbox(
        self,
        *,
        thread_id: str,
        kind: str,
        payload: dict[str, Any],
        expected_state_version: int,
        delay_seconds: float = 0,
    ) -> RuntimeOutboxItem:
        def _tx(conn):
            loaded = _loaded_from_conn(conn, thread_id)
            if loaded.state_version != expected_state_version:
                raise ThreadStateConflict("thread state_version conflict")
            now = _now()
            outbox_id = _uid(kind)
            conn.execute(
                """INSERT INTO runtime_outbox (
                       id, thread_id, kind, payload_json,
                       expected_state_version, available_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    outbox_id,
                    thread_id,
                    kind,
                    _json(payload),
                    expected_state_version,
                    time.time() + delay_seconds,
                    now,
                ),
            )
            return RuntimeOutboxItem(
                outbox_id=outbox_id,
                thread_id=thread_id,
                kind=kind,
                payload=payload,
                expected_state_version=expected_state_version,
            )

        return await self._write(_tx)

    async def claim_outbox(
        self, outbox_id: str, *, lease_owner: str, lease_seconds: float
    ) -> RuntimeOutboxItem | None:
        def _tx(conn):
            now_ts = time.time()
            row = conn.execute(
                """SELECT * FROM runtime_outbox
                   WHERE id = ?
                     AND delivered_at IS NULL
                     AND available_at <= ?
                     AND (claimed_until IS NULL OR claimed_until <= ?)""",
                (outbox_id, now_ts, now_ts),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE runtime_outbox
                   SET claimed_by = ?, claimed_until = ?
                   WHERE id = ?""",
                (lease_owner, now_ts + lease_seconds, row["id"]),
            )
            return _outbox_from_row(row)

        return await self._write(_tx)

    async def claim_next_outbox(
        self, *, lease_owner: str, lease_seconds: float, kind: str | None = None
    ) -> RuntimeOutboxItem | None:
        def _tx(conn):
            now_ts = time.time()
            if kind is None:
                row = conn.execute(
                    """SELECT * FROM runtime_outbox
                       WHERE delivered_at IS NULL
                         AND available_at <= ?
                         AND (claimed_until IS NULL OR claimed_until <= ?)
                       ORDER BY available_at, created_at
                       LIMIT 1""",
                    (now_ts, now_ts),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM runtime_outbox
                       WHERE delivered_at IS NULL
                         AND kind = ?
                         AND available_at <= ?
                         AND (claimed_until IS NULL OR claimed_until <= ?)
                       ORDER BY available_at, created_at
                       LIMIT 1""",
                    (kind, now_ts, now_ts),
                ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE runtime_outbox
                   SET claimed_by = ?, claimed_until = ?
                   WHERE id = ?""",
                (lease_owner, now_ts + lease_seconds, row["id"]),
            )
            return _outbox_from_row(row)

        return await self._write(_tx)

    async def latest_thread_id_with_prefix(self, prefix: str) -> str | None:
        row = await self._one(
            """SELECT id FROM agent_threads
               WHERE id LIKE ?
               ORDER BY created_at DESC, rowid DESC
               LIMIT 1""",
            (f"{prefix}%",),
        )
        return str(row["id"]) if row is not None else None

    async def discard_pending_outbox_prefix(self, prefix: str) -> int:
        """Mark undelivered outbox rows of every thread matching the prefix as delivered."""

        def _tx(conn):
            cur = conn.execute(
                """UPDATE runtime_outbox
                   SET delivered_at = COALESCE(delivered_at, ?)
                   WHERE delivered_at IS NULL AND thread_id LIKE ?""",
                (_now(), f"{prefix}%"),
            )
            return cur.rowcount

        return await self._write(_tx)

    async def discard_pending_outbox(self, thread_id: str) -> int:
        """Mark every undelivered outbox row of the thread as delivered (stop/restart hygiene)."""

        def _tx(conn):
            cur = conn.execute(
                """UPDATE runtime_outbox
                   SET delivered_at = COALESCE(delivered_at, ?)
                   WHERE thread_id = ? AND delivered_at IS NULL""",
                (_now(), thread_id),
            )
            return cur.rowcount

        return await self._write(_tx)

    async def list_pending_outbox(self, thread_id: str) -> list[RuntimeOutboxItem]:
        """Undelivered outbox rows for the thread, regardless of availability."""

        rows = await self._all(
            """SELECT * FROM runtime_outbox
               WHERE thread_id = ? AND delivered_at IS NULL
               ORDER BY available_at, created_at""",
            (thread_id,),
        )
        return [_outbox_from_row(row) for row in rows]

    async def ack_attempt_source_outbox(self, attempt_id: str) -> None:
        def _tx(conn):
            attempt = conn.execute(
                "SELECT source_outbox_id FROM runtime_turn_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            conn.execute(
                "UPDATE runtime_outbox SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?",
                (_now(), attempt["source_outbox_id"]),
            )

        await self._write(_tx)

    async def get_attempt(self, attempt_id: str) -> ThreadAttempt | None:
        row = await self._one(
            "SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)
        )
        return _attempt_from_row(row) if row is not None else None

    async def get_attempt_input_frame(self, attempt_id: str) -> dict[str, Any]:
        row = await self._one(
            "SELECT input_frame_json FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)
        )
        if row is None:
            raise KeyError(attempt_id)
        return json.loads(row["input_frame_json"] or "{}")

    async def set_needs_user_for_attempt(self, attempt_id: str, *, reason: str) -> LoadedThread:
        def _tx(conn):
            attempt = conn.execute(
                "SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            loaded = _loaded_from_conn(conn, attempt["thread_id"])
            decision = RuntimeDecision(
                outcome="needs_user",
                summary="Recovery requires user review.",
                reason=reason,
            )
            state = loaded.state.model_copy(
                update={"lifecycle": LifecycleState.NEEDS_USER, "lifecycle_decision": decision}
            )
            now = _now()
            conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(state), now, loaded.thread.thread_id, loaded.state_version),
            )
            return _loaded_from_conn(conn, loaded.thread.thread_id)

        return await self._write(_tx)

    async def ack_outbox(self, outbox_id: str) -> None:
        def _tx(conn):
            conn.execute(
                "UPDATE runtime_outbox SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?",
                (_now(), outbox_id),
            )

        await self._write(_tx)

    async def release_outbox_claim(self, outbox_id: str) -> None:
        """Drop a claim without delivering: another worker may claim it again."""
        def _tx(conn):
            conn.execute(
                "UPDATE runtime_outbox SET claimed_by = NULL, claimed_until = NULL "
                "WHERE id = ? AND delivered_at IS NULL",
                (outbox_id,),
            )

        await self._write(_tx)


def _outbox_from_row(row) -> RuntimeOutboxItem:
    return RuntimeOutboxItem(
        outbox_id=row["id"],
        thread_id=row["thread_id"],
        kind=row["kind"],
        payload=json.loads(row["payload_json"] or "{}"),
        expected_state_version=int(row["expected_state_version"]),
    )


def _loaded_from_conn(conn, thread_id: str) -> LoadedThread:
    row = conn.execute(
        """SELECT t.*, s.state_json, s.state_version
           FROM agent_threads t
           JOIN agent_thread_state s ON s.thread_id = t.id
           WHERE t.id = ?""",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise KeyError(thread_id)
    state = AgentThreadState.model_validate_json(row["state_json"])
    return LoadedThread(
        thread=AgentThread(
            thread_id=row["id"],
            session_id=row["session_id"],
            parent_thread_id=row["parent_thread_id"],
            workspace=row["workspace"],
            lifecycle=AgentThread.model_fields["lifecycle"].default,
        ),
        profile=RuntimeProfile.model_validate_json(row["profile_json"]),
        state=state,
        state_version=int(row["state_version"]),
        resource_scope=json.loads(row["resource_scope_json"] or "{}"),
    )


def _attempt_from_row(row) -> ThreadAttempt:
    return ThreadAttempt(
        attempt_id=row["id"],
        thread_id=row["thread_id"],
        source_outbox_id=row["source_outbox_id"],
        state_version=int(row["base_state_version"]) + 1,
        fencing_token=int(row["fencing_token"]),
        status=row["status"],
        side_effect_started=bool(row["side_effect_started"]),
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_profile(value: RuntimeProfile) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude={"prompt_policy"}),
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_model(value) -> str:
    return value.model_dump_json()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
