"""Durable store for runtime-owned agent threads and turn attempts."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from voidx.agent.domain.agent_profile import (
    AgentProfileSnapshot,
    ResolvedAgentProfile,
    ResourcePolicy,
    content_hash_of,
)
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.run_config import resolve_run_config
from voidx.agent.domain.thread import (
    AgentThread,
    AgentThreadState,
    LifecycleState,
    RuntimeDecision,
    RuntimeOutboxItem,
    ThreadAttempt,
    apply_lifecycle_decision,
)
from voidx.agent.domain.automation.goal import (
    GoalGenerationBinding,
    GoalProtocolRecord,
    GoalRuntimeFailure,
    GoalSpec,
    PublicSummary,
    GoalState,
    is_goal_terminal,
)
from voidx.agent.domain.guidance import Guidance
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.ports.persistence import (
    GoalProtocolConflict,
    GuidanceConflict,
    ThreadStateConflict,
)
from voidx.agent.adapters.persistence.session_models import (
    SessionInfo,
    snapshot_columns,
    snapshot_from_row,
    validate_runtime_profile,
)
from voidx.persistence.jsonl import (
    append_session_record,
    delete_session_directories,
    read_session_records,
    session_directory_locks,
)
from voidx.persistence.sqlite import (
    fetch_all,
    fetch_one,
    now,
    write_transaction,
    fetch_all_on,
    fetch_one_on,
    open_isolated_db,
    write_transaction_on,
)


@dataclass(frozen=True)
class LoadedThread:
    thread: AgentThread
    resolved_profile: ResolvedAgentProfile
    state: AgentThreadState
    state_version: int
    resource_scope: dict[str, Any]

    @property
    def profile(self) -> RuntimeProfile:
        return self.resolved_profile.runtime_profile


@dataclass(frozen=True)
class CommitResult:
    state_version: int
    lifecycle: str
    next_outbox_id: str | None = None


class ThreadStore:
    def __init__(self, db_path: Any = None) -> None:
        # db_path given → isolated database (tests/tools); None → shared global store.
        self._conn = open_isolated_db(db_path) if db_path is not None else None
        self._cleanup_bindings: dict[str, GoalGenerationBinding] = {}

    def _write(self, tx: Any) -> Any:
        if self._conn is not None:
            return write_transaction_on(self._conn, tx)
        return write_transaction(tx)

    def _one(self, sql: str, params: tuple = ()) -> Any:
        if self._conn is not None:
            return fetch_one_on(self._conn, sql, params)
        return fetch_one(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> Any:
        if self._conn is not None:
            return fetch_all_on(self._conn, sql, params)
        return fetch_all(sql, params)

    async def submit_guidance(self, guidance: Guidance) -> Guidance:
        if not isinstance(guidance, Guidance):
            raise TypeError("guidance must be a Guidance")

        def _tx(conn):
            cleanup = _cleanup_tombstone_for_target(
                conn,
                generation=guidance.target_run_id or "",
                thread_id=guidance.target_thread_id or "",
                session_id=guidance.target_session_id or "",
            )
            if cleanup is not None:
                raise GoalProtocolConflict("Goal generation cleanup is in progress")
            archived = conn.execute(
                """SELECT generation FROM goal_generations
                   WHERE archived_at IS NOT NULL
                     AND (
                         generation = ?
                         OR goal_thread_id = ?
                         OR work_session_id = ?
                         OR evaluator_session_id = ?
                     )
                   LIMIT 1""",
                (
                    guidance.target_run_id or "",
                    guidance.target_thread_id or "",
                    guidance.target_session_id or "",
                    guidance.target_session_id or "",
                ),
            ).fetchone()
            if archived is not None:
                raise GoalProtocolConflict("Goal generation is archived")
            existing = conn.execute(
                "SELECT * FROM guidance_inbox WHERE guidance_id = ?",
                (guidance.guidance_id,),
            ).fetchone()
            if existing is not None:
                current = _guidance_from_row(existing)
                if _guidance_immutable_payload(current) != _guidance_immutable_payload(guidance):
                    raise GuidanceConflict(
                        f"guidance id conflict: {guidance.guidance_id}"
                    )
                return current
            conn.execute(
                """INSERT INTO guidance_inbox (
                       guidance_id, text, truncated, source, created_at,
                       target_session_id, target_thread_id, target_run_id,
                       target_phase, delivery_id, delivered_phase, consumed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guidance.guidance_id,
                    guidance.text,
                    int(guidance.truncated),
                    guidance.source,
                    guidance.created_at.isoformat(),
                    guidance.target_session_id,
                    guidance.target_thread_id,
                    guidance.target_run_id,
                    guidance.target_phase,
                    guidance.delivery_id,
                    guidance.delivered_phase,
                    guidance.consumed_at.isoformat() if guidance.consumed_at else None,
                ),
            )
            return guidance

        return await self._write(_tx)

    async def get_guidance(self, guidance_id: str) -> Guidance | None:
        row = await self._one(
            "SELECT * FROM guidance_inbox WHERE guidance_id = ?", (guidance_id,)
        )
        return _guidance_from_row(row) if row is not None else None

    async def bind_guidance(
        self,
        delivery_id: str,
        *,
        session_id: str = "",
        thread_id: str = "",
        run_id: str = "",
        phase: str | None = None,
    ) -> list[Guidance]:
        delivery_id = delivery_id.strip()
        if not delivery_id:
            raise ValueError("delivery_id must not be empty")
        session_id = session_id.strip()
        thread_id = thread_id.strip()
        run_id = run_id.strip()
        phase = phase.strip() if phase else None

        def _tx(conn):
            rows = conn.execute(
                """SELECT * FROM guidance_inbox
                   WHERE delivery_id = ? AND consumed_at IS NULL
                   ORDER BY created_at, guidance_id""",
                (delivery_id,),
            ).fetchall()
            bound = [_guidance_from_row(row) for row in rows]
            candidates = conn.execute(
                """SELECT * FROM guidance_inbox
                   WHERE consumed_at IS NULL AND delivery_id IS NULL
                   ORDER BY created_at, guidance_id"""
            ).fetchall()
            timestamp_phase = phase
            for row in candidates:
                guidance = _guidance_from_row(row)
                if not _guidance_matches(
                    guidance,
                    session_id=session_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    phase=phase,
                ):
                    continue
                updated = conn.execute(
                    """UPDATE guidance_inbox
                       SET delivery_id = ?, delivered_phase = ?
                       WHERE guidance_id = ?
                         AND delivery_id IS NULL AND consumed_at IS NULL""",
                    (delivery_id, timestamp_phase, guidance.guidance_id),
                )
                if updated.rowcount != 1:
                    continue
                refreshed = conn.execute(
                    "SELECT * FROM guidance_inbox WHERE guidance_id = ?",
                    (guidance.guidance_id,),
                ).fetchone()
                bound.append(_guidance_from_row(refreshed))
            return bound

        return await self._write(_tx)

    async def release_guidance(self, delivery_id: str) -> None:
        delivery_id = delivery_id.strip()
        if not delivery_id:
            raise ValueError("delivery_id must not be empty")

        def _tx(conn):
            conn.execute(
                """UPDATE guidance_inbox
                   SET delivery_id = NULL, delivered_phase = NULL
                   WHERE delivery_id = ? AND consumed_at IS NULL""",
                (delivery_id,),
            )

        await self._write(_tx)

    async def consume_guidance(self, delivery_id: str) -> None:
        delivery_id = delivery_id.strip()
        if not delivery_id:
            raise ValueError("delivery_id must not be empty")

        def _tx(conn):
            conn.execute(
                """UPDATE guidance_inbox
                   SET consumed_at = COALESCE(consumed_at, ?)
                   WHERE delivery_id = ? AND consumed_at IS NULL""",
                (now(), delivery_id),
            )

        await self._write(_tx)



    async def acquire_goal_generation_lease(
        self,
        generation: str,
        lease_owner: str,
        *,
        lease_seconds: float,
    ) -> bool:
        generation = generation.strip()
        lease_owner = lease_owner.strip()
        if not generation or not lease_owner:
            raise ValueError("generation and lease_owner must not be empty")

        def _tx(conn):
            _ensure_goal_generation_writable(conn, generation)
            binding = conn.execute(
                "SELECT archived_at FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if binding is not None and binding["archived_at"] is not None:
                raise GoalProtocolConflict("Goal generation is archived")
            timestamp = now()
            expires_at = time.time() + lease_seconds
            conn.execute(
                """INSERT OR IGNORE INTO goal_recovery_leases (
                       generation, lease_owner, lease_expires_at, updated_at
                   ) VALUES (?, ?, ?, ?)""",
                (generation, lease_owner, expires_at, timestamp),
            )
            cursor = conn.execute(
                """UPDATE goal_recovery_leases
                   SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                   WHERE generation = ?
                     AND (lease_owner = ? OR lease_expires_at <= ?)""",
                (
                    lease_owner,
                    expires_at,
                    timestamp,
                    generation,
                    lease_owner,
                    time.time(),
                ),
            )
            return cursor.rowcount == 1

        return await self._write(_tx)


    async def renew_goal_generation_lease(
        self,
        generation: str,
        lease_owner: str,
        *,
        lease_seconds: float,
    ) -> bool:
        generation = generation.strip()
        lease_owner = lease_owner.strip()
        if not generation or not lease_owner:
            raise ValueError("generation and lease_owner must not be empty")

        def _tx(conn):
            cursor = conn.execute(
                """UPDATE goal_recovery_leases
                   SET lease_expires_at = ?, updated_at = ?
                   WHERE generation = ? AND lease_owner = ? AND lease_expires_at > ?""",
                (time.time() + lease_seconds, now(), generation, lease_owner, time.time()),
            )
            return cursor.rowcount == 1

        return await self._write(_tx)
    async def release_goal_generation_lease(
        self,
        generation: str,
        lease_owner: str,
    ) -> bool:
        generation = generation.strip()
        lease_owner = lease_owner.strip()
        if not generation or not lease_owner:
            raise ValueError("generation and lease_owner must not be empty")

        def _tx(conn):
            cursor = conn.execute(
                "DELETE FROM goal_recovery_leases WHERE generation = ? AND lease_owner = ?",
                (generation, lease_owner),
            )
            return cursor.rowcount == 1

        return await self._write(_tx)

    async def get_goal_generation_lease(self, generation: str) -> dict[str, Any] | None:
        row = await self._one(
            "SELECT * FROM goal_recovery_leases WHERE generation = ?",
            (generation.strip(),),
        )
        return dict(row) if row is not None else None
    async def ensure_session(
        self,
        session_id: str,
        workspace: str,
        *,
        profile: str = "coding",
        title: str = "Loop session",
        root_session_id: str | None = None,
        profile_snapshot: AgentProfileSnapshot | None = None,
    ) -> None:
        from voidx.llm.domain.model import DEFAULT_MODEL

        profile = validate_runtime_profile(profile)
        revision, content_hash, snapshot_hash, source, payload_json = snapshot_columns(
            profile_snapshot
        )

        timestamp = now()

        def _tx(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (
                       id, title, workspace, directory, model_provider, model_name,
                       runtime_profile, runtime_profile_revision,
                       runtime_profile_content_hash, runtime_profile_hash,
                       runtime_profile_source, runtime_profile_snapshot,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    title,
                    workspace,
                    workspace,
                    "anthropic",
                    DEFAULT_MODEL,
                    profile,
                    revision,
                    content_hash,
                    snapshot_hash,
                    source,
                    payload_json,
                    timestamp,
                    timestamp,
                ),
            )
            if root_session_id is None:
                return
            root = conn.execute(
                "SELECT root_session_id, owner_id FROM provisional_sessions WHERE session_id = ?",
                (root_session_id,),
            ).fetchone()
            if root is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO provisional_sessions (
                           session_id, root_session_id, owner_id, created_at
                       ) VALUES (?, ?, ?, ?)""",
                    (session_id, root["root_session_id"], root["owner_id"], timestamp),
                )

        await self._write(_tx)

    async def get_session(self, session_id: str):

        row = await self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return SessionInfo(
            id=row["id"],
            title=row["title"],
            workspace=row["workspace"],
            directory=row["directory"],
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["message_count"],
            runtime_profile=row["runtime_profile"],
            profile_snapshot=snapshot_from_row(row),
        )

    async def initialize_goal_generation(
        self,
        *,
        generation: str,
        main_session_id: str,
        evaluator_session_id: str,
        work_session_id: str,
        goal_thread_id: str,
        parent_thread_id: str,
        workspace: str,
        profile_id: str,
        profile_snapshot: AgentProfileSnapshot,
        thread_profile: ResolvedAgentProfile | RuntimeProfile,
        thread_state: AgentThreadState,
        protocol: GoalProtocolRecord,
    ) -> GoalGenerationBinding:
        """Atomically commit Goal boundary I and its first work wakeup."""
        from voidx.platform.session_ids import validate_session_storage_id

        for session_id in (main_session_id, evaluator_session_id, work_session_id):
            validate_session_storage_id(session_id)
        if not generation.strip():
            raise ValueError("generation must not be empty")
        if thread_state.thread_id != goal_thread_id:
            raise ValueError("thread state id must match goal thread id")
        if (
            protocol.generation != generation
            or protocol.phase != "init"
            or protocol.attempt_number != 0
            or protocol.sequence_number != 0
        ):
            raise GoalProtocolConflict("Boundary I requires INIT at sequence 0")
        if protocol.payload_type != "GoalSpecSnapshot":
            raise GoalProtocolConflict("Boundary I requires GoalSpecSnapshot")
        resolved_profile = _as_resolved_profile(thread_profile)
        if resolved_profile.runtime_profile.profile_id != profile_id:
            raise GoalProtocolConflict("Goal profile binding conflict")
        if profile_snapshot.profile_id != profile_id:
            raise GoalProtocolConflict("Goal profile snapshot conflict")

        def _tx(conn):
            _ensure_goal_generation_writable(conn, generation)
            existing = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["main_session_id"] != main_session_id
                    or existing["evaluator_session_id"] != evaluator_session_id
                    or existing["work_session_id"] != work_session_id
                    or existing["goal_thread_id"] != goal_thread_id
                ):
                    raise GoalProtocolConflict("Goal generation binding conflict")
                return _goal_generation_from_row(existing)

            main = conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (main_session_id,)
            ).fetchone()
            if main is None:
                raise GoalProtocolConflict("main session must exist before Boundary I")

            for session_id, title in (
                (evaluator_session_id, "Goal evaluator session"),
                (work_session_id, "Goal work session"),
            ):
                row = conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is not None:
                    raise GoalProtocolConflict(
                        f"Goal session id already exists: {session_id}"
                    )
                _insert_goal_session(
                    conn,
                    session_id=session_id,
                    workspace=workspace,
                    profile_id=profile_id,
                    profile_snapshot=profile_snapshot,
                    title=title,
                )

            used = conn.execute(
                """SELECT generation FROM goal_generations
                   WHERE evaluator_session_id IN (?, ?)
                      OR work_session_id IN (?, ?)""",
                (evaluator_session_id, work_session_id, evaluator_session_id, work_session_id),
            ).fetchone()
            if used is not None:
                raise GoalProtocolConflict("Goal session is already bound to a generation")

            timestamp = now()
            conn.execute(
                """INSERT INTO goal_generations (
                       generation, main_session_id, evaluator_session_id,
                       work_session_id, goal_thread_id, visibility, created_at
                   ) VALUES (?, ?, ?, ?, ?, 'internal', ?)""",
                (
                    generation,
                    main_session_id,
                    evaluator_session_id,
                    work_session_id,
                    goal_thread_id,
                    timestamp,
                ),
            )

            existing_protocol = conn.execute(
                """SELECT * FROM goal_protocol_records
                   WHERE generation = ? AND sequence_number = 0""",
                (generation,),
            ).fetchone()
            projected_at = now()
            if existing_protocol is not None:
                existing_record = _goal_protocol_from_row(existing_protocol)
                if existing_record.payload_hash != protocol.payload_hash:
                    raise GoalProtocolConflict("INIT payload conflict")
                conn.execute(
                    """UPDATE goal_protocol_records
                       SET status = 'projected', projected_at = ?
                       WHERE protocol_id = ?""",
                    (projected_at, existing_record.protocol_id),
                )
            else:
                conn.execute(
                    """INSERT INTO goal_protocol_records (
                           protocol_id, parent_session_id, generation, phase,
                           attempt_number, sequence_number, turn_id, session_id,
                           payload_type, payload_json, status, payload_hash,
                           submitted_at, projected_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        protocol.protocol_id,
                        protocol.parent_session_id,
                        protocol.generation,
                        protocol.phase,
                        protocol.attempt_number,
                        protocol.sequence_number,
                        protocol.turn_id,
                        protocol.session_id,
                        protocol.payload_type,
                        _json(protocol.payload),
                        "projected",
                        protocol.payload_hash,
                        protocol.submitted_at.isoformat(),
                        projected_at,
                    ),
                )

            goal_state = GoalState.model_validate(
                thread_state.context.get("goal_run") or {}
            ).model_copy(
                update={
                    "generation": generation,
                    "main_session_id": main_session_id,
                    "work_session_id": work_session_id,
                    "evaluator_session_id": evaluator_session_id,
                    "projected_sequence_number": 0,
                    "current_phase": "work",
                    "phase_status": "running",
                    "last_protocol_id": protocol.protocol_id,
                }
            )
            projected_thread_state = thread_state.model_copy(
                update={"context": {**thread_state.context, "goal_run": goal_state.model_dump(mode="json")}}
            )
            conn.execute(
                """INSERT INTO agent_threads (
                       id, parent_thread_id, session_id, workspace, profile_id,
                       profile_revision, profile_json, resource_scope_json,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    goal_thread_id,
                    parent_thread_id,
                    work_session_id,
                    workspace,
                    resolved_profile.runtime_profile.profile_id,
                    resolved_profile.runtime_profile.revision,
                    _json_profile(resolved_profile),
                    "{}",
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """INSERT INTO agent_thread_state (
                       thread_id, state_json, state_version, updated_at
                   ) VALUES (?, ?, 0, ?)""",
                (goal_thread_id, _json_model(projected_thread_state), timestamp),
            )
            snapshot = protocol.payload
            spec = dict(snapshot)
            outbox_payload = {
                "phase": "work",
                "generation": generation,
                "attempt_number": 1,
                "sequence_number": 1,
                "spec": {
                    key: spec[key]
                    for key in (
                        "objective",
                        "acceptance_condition",
                        "achievement_method",
                        "max_attempts",
                        "workflow_enabled",
                        "generation",
                    )
                    if key in spec
                },
                "goal_state": goal_state.model_dump(mode="json"),
            }
            outbox_id = _uid("goal-work")
            conn.execute(
                """INSERT INTO runtime_outbox (
                       id, thread_id, kind, payload_json,
                       expected_state_version, available_at, created_at
                   ) VALUES (?, ?, 'goal_prompt', ?, 0, ?, ?)""",
                (
                    outbox_id,
                    goal_thread_id,
                    _json(outbox_payload),
                    time.time(),
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            return _goal_generation_from_row(row)

        return await self._write(_tx)

    async def get_goal_generation(self, generation: str) -> GoalGenerationBinding | None:
        row = await self._one(
            "SELECT * FROM goal_generations WHERE generation = ?", (generation,)
        )
        return _goal_generation_from_row(row) if row is not None else None

    async def get_goal_runtime_failure(
        self, generation: str
    ) -> GoalRuntimeFailure | None:
        row = await self._one(
            "SELECT * FROM goal_runtime_failures WHERE generation = ?",
            (generation.strip(),),
        )
        return _goal_runtime_failure_from_row(row) if row is not None else None

    async def list_goal_public_summaries(
        self, main_session_id: str
    ) -> list[dict[str, Any]]:
        rows = await self._all(
            """SELECT * FROM goal_public_summary_outbox
               WHERE main_session_id = ? ORDER BY created_at, summary_id""",
            (main_session_id.strip(),),
        )
        return [_goal_public_summary_row(row) for row in rows]

    async def list_pending_goal_public_summaries(
        self,
        *,
        main_session_id: str = "",
        generation: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["delivered_at IS NULL"]
        params: list[object] = []
        if main_session_id:
            clauses.append("main_session_id = ?")
            params.append(main_session_id.strip())
        if generation:
            clauses.append("generation = ?")
            params.append(generation.strip())
        rows = await self._all(
            "SELECT * FROM goal_public_summary_outbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at, summary_id",
            tuple(params),
        )
        return [_goal_public_summary_row(row) for row in rows]

    async def _ack_goal_public_summary_delivery(
        self, summary_id: str, message_id: int
    ) -> bool:
        def _tx(conn):
            row = conn.execute(
                "SELECT * FROM goal_public_summary_outbox WHERE summary_id = ?",
                (summary_id,),
            ).fetchone()
            if row is None:
                raise KeyError(summary_id)
            if row["delivered_at"] is not None:
                return False
            timestamp = now()
            updated = conn.execute(
                """UPDATE goal_public_summary_outbox SET delivered_at = ?
                   WHERE summary_id = ? AND delivered_at IS NULL""",
                (timestamp, summary_id),
            )
            if updated.rowcount != 1:
                return False
            session = conn.execute(
                "SELECT message_count FROM sessions WHERE id = ?",
                (row["main_session_id"],),
            ).fetchone()
            if session is None:
                raise GoalProtocolConflict("Goal public summary main session is missing")
            conn.execute(
                """UPDATE sessions
                   SET message_count = message_count + 1, updated_at = ?
                   WHERE id = ?""",
                (timestamp, row["main_session_id"]),
            )
            return True

        return await self._write(_tx)

    async def deliver_goal_public_summary(self, summary_id: str) -> bool:
        row = await self._one(
            "SELECT * FROM goal_public_summary_outbox WHERE summary_id = ?",
            (summary_id.strip(),),
        )
        if row is None:
            raise KeyError(summary_id)
        if row["delivered_at"] is not None:
            return False
        summary = _goal_public_summary_row(row)
        session_id = summary["main_session_id"]
        marker_key = "goal_public_summary_id"

        async with session_directory_locks((session_id,)):
            records = await read_session_records(session_id, "messages.jsonl") or []
            existing = next(
                (
                    record
                    for record in records
                    if (record.get("additional_kwargs") or {}).get(marker_key)
                    == summary_id
                ),
                None,
            )
            if existing is None:
                session = await self._one(
                    "SELECT message_count FROM sessions WHERE id = ?",
                    (session_id,),
                )
                if session is None:
                    raise GoalProtocolConflict("Goal public summary main session is missing")
                ids = [
                    int(record["id"])
                    for record in records
                    if record.get("type") == "message"
                    and isinstance(record.get("id"), int)
                ]
                message_id = max(
                    int(session["message_count"] or 0) + 1,
                    max(ids, default=0) + 1,
                )
                await append_session_record(
                    session_id,
                    "messages.jsonl",
                    {
                        "type": "message",
                        "id": message_id,
                        "role": "assistant",
                        "content": summary["summary"],
                        "content_format": "text",
                        "created_at": summary["created_at"],
                        "additional_kwargs": {
                            marker_key: summary_id,
                            "goal_public_summary": summary["payload"],
                        },
                    },
                )
            else:
                message_id = int(existing["id"])
            return await self._ack_goal_public_summary_delivery(
                summary_id,
                message_id,
            )

    async def deliver_goal_public_summaries(
        self,
        *,
        main_session_id: str = "",
        generation: str = "",
    ) -> int:
        if not main_session_id and not generation:
            raise ValueError("Goal public summary delivery requires a session or generation")
        pending = await self.list_pending_goal_public_summaries(
            main_session_id=main_session_id,
            generation=generation,
        )
        delivered = 0
        for summary in pending:
            delivered += int(
                await self.deliver_goal_public_summary(summary["summary_id"])
            )
        return delivered

    async def fail_goal_generation(
        self, failure: GoalRuntimeFailure
    ) -> GoalRuntimeFailure:
        if not isinstance(failure, GoalRuntimeFailure):
            raise TypeError("failure must be a GoalRuntimeFailure")

        def _tx(conn):
            existing = conn.execute(
                "SELECT * FROM goal_runtime_failures WHERE generation = ?",
                (failure.generation,),
            ).fetchone()
            if existing is not None:
                current = _goal_runtime_failure_from_row(existing)
                if current != failure:
                    raise GoalProtocolConflict("Goal runtime failure conflict")
                return current
            _ensure_goal_generation_writable(conn, failure.generation)
            binding = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (failure.generation,),
            ).fetchone()
            if binding is None:
                raise KeyError(failure.generation)
            goal_thread_id = binding["goal_thread_id"]
            if not goal_thread_id:
                raise GoalProtocolConflict("Goal generation has no goal thread")
            state_row = conn.execute(
                """SELECT state_json, state_version FROM agent_thread_state
                   WHERE thread_id = ?""",
                (goal_thread_id,),
            ).fetchone()
            if state_row is None:
                raise GoalProtocolConflict("Goal thread state is missing")
            thread_state = AgentThreadState.model_validate_json(
                state_row["state_json"]
            )
            if is_goal_terminal(thread_state.lifecycle):
                raise GoalProtocolConflict("Goal generation is already terminal")

            timestamp = now()
            conn.execute(
                """INSERT INTO goal_runtime_failures (
                       generation, observed_sequence, reason, evidence_json,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    failure.generation,
                    failure.observed_sequence,
                    failure.reason,
                    _json(list(failure.evidence)),
                    failure.created_at.isoformat(),
                ),
            )
            failed_state = thread_state.model_copy(
                update={
                    "lifecycle": LifecycleState.FAILED,
                    "lifecycle_decision": RuntimeDecision(
                        outcome="failed",
                        summary="Goal runtime stopped after an internal consistency failure.",
                        reason=failure.reason,
                    ),
                }
            )
            updated = conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1,
                       updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (
                    _json_model(failed_state),
                    timestamp,
                    goal_thread_id,
                    state_row["state_version"],
                ),
            )
            if updated.rowcount != 1:
                raise ThreadStateConflict("Goal runtime failure state race")
            conn.execute(
                "UPDATE agent_threads SET updated_at = ? WHERE id = ?",
                (timestamp, goal_thread_id),
            )
            conn.execute(
                """UPDATE runtime_turn_attempts
                   SET status = 'committed', lease_owner = '',
                       lease_expires_at = 0, updated_at = ?
                   WHERE thread_id = ? AND status = 'prepared'""",
                (timestamp, goal_thread_id),
            )
            conn.execute(
                """UPDATE runtime_outbox
                   SET delivered_at = COALESCE(delivered_at, ?),
                       claimed_by = NULL, claimed_until = NULL
                   WHERE thread_id = ? AND delivered_at IS NULL""",
                (timestamp, goal_thread_id),
            )
            conn.execute(
                "DELETE FROM goal_recovery_leases WHERE generation = ?",
                (failure.generation,),
            )
            conn.execute(
                """UPDATE goal_generations
                   SET terminal_at = COALESCE(terminal_at, ?)
                   WHERE generation = ?""",
                (timestamp, failure.generation),
            )
            public_summary = _public_summary_for_failure(
                failure=failure,
                thread_state=thread_state,
                timestamp=timestamp,
            )
            _insert_goal_public_summary_tx(
                conn,
                generation=failure.generation,
                main_session_id=binding["main_session_id"],
                kind="runtime_failure",
                summary=public_summary,
            )
            return failure

        return await self._write(_tx)

    async def archive_goal_generation(self, generation: str) -> GoalGenerationBinding:
        generation = generation.strip()
        if not generation:
            raise ValueError("generation must not be empty")

        def _tx(conn):
            row = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if row is None:
                raise KeyError(generation)
            if row["terminal_at"] is None:
                raise GoalProtocolConflict(
                    "Goal generation must be terminal before archive"
                )
            archived_at = row["archived_at"] or now()
            if row["archived_at"] is None:
                conn.execute(
                    "UPDATE goal_generations SET archived_at = ? WHERE generation = ?",
                    (archived_at, generation),
                )
            updated = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            return _goal_generation_from_row(updated)

        return await self._write(_tx)

    async def cleanup_goal_generation(self, generation: str) -> GoalGenerationBinding:
        generation = generation.strip()
        if not generation:
            raise ValueError("generation must not be empty")

        def _prepare(conn):
            tombstone = conn.execute(
                "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
                (generation,),
            ).fetchone()
            if tombstone is not None and tombstone["status"] == "committed":
                return {"status": "committed", "tombstone": tombstone, "binding": None}

            row = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if row is None:
                if tombstone is not None:
                    return {"status": "pending", "tombstone": tombstone, "binding": None}
                raise KeyError(generation)
            if row["terminal_at"] is None:
                raise GoalProtocolConflict(
                    "Goal generation must be terminal before cleanup"
                )
            archived_at = row["archived_at"] or now()
            if row["archived_at"] is None:
                conn.execute(
                    "UPDATE goal_generations SET archived_at = ? WHERE generation = ?",
                    (archived_at, generation),
                )
                row = conn.execute(
                    "SELECT * FROM goal_generations WHERE generation = ?",
                    (generation,),
                ).fetchone()
            binding = _goal_generation_from_row(row)
            if tombstone is None:
                requested_at = now()
                conn.execute(
                    """INSERT INTO goal_generation_cleanup (
                           generation, cleanup_epoch, main_session_id,
                           work_session_id, evaluator_session_id, status,
                           requested_at, completed_at, last_error
                       ) VALUES (?, 1, ?, ?, ?, 'pending', ?, NULL, '')""",
                    (
                        generation,
                        binding.main_session_id,
                        binding.work_session_id,
                        binding.evaluator_session_id,
                        requested_at,
                    ),
                )
                tombstone = conn.execute(
                    "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
                    (generation,),
                ).fetchone()
            return {"status": "pending", "tombstone": tombstone, "binding": binding}

        prepared = await self._write(_prepare)
        tombstone = prepared["tombstone"]
        cached = self._cleanup_bindings.get(generation)
        if prepared["status"] == "committed":
            if cached is not None:
                return cached
            return _binding_from_cleanup_tombstone(tombstone)

        binding = prepared["binding"]
        work_session_id = binding.work_session_id if binding is not None else tombstone["work_session_id"]
        evaluator_session_id = (
            binding.evaluator_session_id if binding is not None else tombstone["evaluator_session_id"]
        )
        session_ids = (work_session_id, evaluator_session_id)

        try:
            async with session_directory_locks(session_ids):
                await delete_session_directories(session_ids)

                def _commit(conn):
                    current = conn.execute(
                        "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
                        (generation,),
                    ).fetchone()
                    if current is None:
                        raise KeyError(generation)
                    if current["status"] == "committed":
                        return _binding_from_cleanup_tombstone(current)

                    current_binding = conn.execute(
                        "SELECT * FROM goal_generations WHERE generation = ?",
                        (generation,),
                    ).fetchone()
                    goal_thread_id = (
                        current_binding["goal_thread_id"]
                        if current_binding is not None
                        else (binding.goal_thread_id if binding is not None else None)
                    )
                    thread_ids = [row["id"] for row in conn.execute(
                        "SELECT id FROM agent_threads WHERE session_id IN (?, ?)",
                        (work_session_id, evaluator_session_id),
                    ).fetchall()]
                    if goal_thread_id and goal_thread_id not in thread_ids:
                        thread_ids.append(goal_thread_id)
                    if thread_ids:
                        placeholders = ", ".join("?" for _ in thread_ids)
                        conn.execute(
                            f"DELETE FROM runtime_outbox WHERE thread_id IN ({placeholders})",
                            tuple(thread_ids),
                        )
                        conn.execute(
                            f"DELETE FROM runtime_turn_attempts WHERE thread_id IN ({placeholders})",
                            tuple(thread_ids),
                        )
                        for table in (
                            "agent_thread_messages",
                            "agent_thread_frames",
                            "agent_thread_state",
                            "agent_threads",
                        ):
                            conn.execute(
                                f"DELETE FROM {table} WHERE id IN ({placeholders})"
                                if table == "agent_threads"
                                else f"DELETE FROM {table} WHERE thread_id IN ({placeholders})",
                                tuple(thread_ids),
                            )
                    conn.execute(
                        """DELETE FROM guidance_inbox
                           WHERE target_thread_id = ?
                              OR target_run_id = ?
                              OR target_session_id IN (?, ?)""",
                        (
                            goal_thread_id or "",
                            generation,
                            work_session_id,
                            evaluator_session_id,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM goal_recovery_leases WHERE generation = ?",
                        (generation,),
                    )
                    conn.execute(
                        "DELETE FROM goal_transcript_records WHERE generation = ?",
                        (generation,),
                    )
                    conn.execute(
                        "DELETE FROM goal_runtime_failures WHERE generation = ?",
                        (generation,),
                    )
                    conn.execute(
                        "DELETE FROM goal_protocol_records WHERE generation = ?",
                        (generation,),
                    )
                    conn.execute(
                        "DELETE FROM goal_generations WHERE generation = ?",
                        (generation,),
                    )
                    conn.execute(
                        "DELETE FROM sessions WHERE id IN (?, ?)",
                        (work_session_id, evaluator_session_id),
                    )
                    completed_at = now()
                    conn.execute(
                        """UPDATE goal_generation_cleanup
                           SET status = 'committed', completed_at = ?, last_error = ''
                           WHERE generation = ?""",
                        (completed_at, generation),
                    )
                    return current_binding

                await self._write(_commit)
        except Exception as exc:
            error = str(exc)
            try:
                await self._write(
                    lambda conn: conn.execute(
                        """UPDATE goal_generation_cleanup
                           SET last_error = ?
                           WHERE generation = ? AND status = 'pending'""",
                        (error, generation),
                    )
                )
            except Exception:
                pass
            raise

        result = binding or _binding_from_cleanup_tombstone(tombstone)
        self._cleanup_bindings[generation] = result
        return result

    async def list_goal_generations(self, main_session_id: str) -> list[GoalGenerationBinding]:
        rows = await self._all(
            """SELECT * FROM goal_generations
               WHERE main_session_id = ? ORDER BY created_at""",
            (main_session_id,),
        )
        return [_goal_generation_from_row(row) for row in rows]

    async def prepare_goal_generation_cleanup(
        self,
        generation: str,
        *,
        reason: str,
    ) -> GoalGenerationBinding:
        generation = generation.strip()
        reason = reason.strip()
        if not generation or not reason:
            raise ValueError("generation and cleanup reason must not be empty")

        def _tx(conn):
            existing_cleanup = conn.execute(
                "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
                (generation,),
            ).fetchone()
            if existing_cleanup is not None:
                binding_row = conn.execute(
                    "SELECT * FROM goal_generations WHERE generation = ?",
                    (generation,),
                ).fetchone()
                if binding_row is not None:
                    return _goal_generation_from_row(binding_row)
                return _binding_from_cleanup_tombstone(existing_cleanup)

            binding_row = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if binding_row is None:
                raise KeyError(generation)
            binding = _goal_generation_from_row(binding_row)
            timestamp = now()
            if binding.goal_thread_id:
                state_row = conn.execute(
                    """SELECT state_json, state_version FROM agent_thread_state
                       WHERE thread_id = ?""",
                    (binding.goal_thread_id,),
                ).fetchone()
                if state_row is None:
                    raise GoalProtocolConflict("Goal thread state is missing")
                thread_state = AgentThreadState.model_validate_json(state_row["state_json"])
                if not is_goal_terminal(thread_state.lifecycle):
                    cancelled = thread_state.model_copy(
                        update={
                            "lifecycle": LifecycleState.CANCELLED,
                            "lifecycle_decision": RuntimeDecision(
                                outcome="stop",
                                summary=reason,
                                progress="partial",
                                reason="main_session_deleted",
                            ),
                        }
                    )
                    updated = conn.execute(
                        """UPDATE agent_thread_state
                           SET state_json = ?, state_version = state_version + 1,
                               updated_at = ?
                           WHERE thread_id = ? AND state_version = ?""",
                        (
                            _json_model(cancelled),
                            timestamp,
                            binding.goal_thread_id,
                            state_row["state_version"],
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ThreadStateConflict("Goal cleanup cancellation race")
                conn.execute(
                    """UPDATE runtime_outbox
                       SET delivered_at = COALESCE(delivered_at, ?),
                           claimed_by = NULL, claimed_until = NULL
                       WHERE thread_id = ? AND delivered_at IS NULL""",
                    (timestamp, binding.goal_thread_id),
                )
            conn.execute(
                "DELETE FROM goal_recovery_leases WHERE generation = ?",
                (generation,),
            )
            conn.execute(
                """UPDATE goal_generations
                   SET terminal_at = COALESCE(terminal_at, ?),
                       archived_at = COALESCE(archived_at, ?)
                   WHERE generation = ?""",
                (timestamp, timestamp, generation),
            )
            conn.execute(
                """INSERT INTO goal_generation_cleanup (
                       generation, cleanup_epoch, main_session_id,
                       work_session_id, evaluator_session_id, status,
                       requested_at, completed_at, last_error
                   ) VALUES (?, 1, ?, ?, ?, 'pending', ?, NULL, '')""",
                (
                    generation,
                    binding.main_session_id,
                    binding.work_session_id,
                    binding.evaluator_session_id,
                    timestamp,
                ),
            )
            updated_binding = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            return _goal_generation_from_row(updated_binding)

        return await self._write(_tx)

    async def prepare_orphan_goal_generation_cleanups(self) -> list[str]:
        rows = await self._all(
            """SELECT g.generation
               FROM goal_generations AS g
               LEFT JOIN sessions AS s ON s.id = g.main_session_id
               LEFT JOIN goal_generation_cleanup AS c
                 ON c.generation = g.generation
               WHERE s.id IS NULL AND c.generation IS NULL
               ORDER BY g.created_at, g.generation"""
        )
        generations: list[str] = []
        for row in rows:
            generation = str(row["generation"])
            await self.prepare_goal_generation_cleanup(
                generation,
                reason="Goal bundle orphaned because its main session is missing.",
            )
            generations.append(generation)
        return generations

    async def list_goal_cleanup_tombstones(self) -> list[dict[str, Any]]:
        rows = await self._all(
            """SELECT * FROM goal_generation_cleanup
               ORDER BY requested_at, generation"""
        )
        return [dict(row) for row in rows]

    async def reconcile_goal_cleanup(self, generation: str) -> GoalGenerationBinding:
        generation = generation.strip()
        if not generation:
            raise ValueError("generation must not be empty")
        row = await self._one(
            "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
            (generation,),
        )
        if row is None:
            raise KeyError(generation)
        if row["status"] == "pending":
            return await self.cleanup_goal_generation(generation)
        session_ids = (row["work_session_id"], row["evaluator_session_id"])
        async with session_directory_locks(session_ids):
            await delete_session_directories(session_ids)
        return self._cleanup_bindings.get(generation) or _binding_from_cleanup_tombstone(row)
    async def ensure_goal_phase_outbox(self, generation: str) -> RuntimeOutboxItem | None:
        """Ensure the next valid Goal phase outbox exists without dispatching it."""
        def _tx(conn):
            _ensure_goal_generation_writable(conn, generation)
            binding_row = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (generation,),
            ).fetchone()
            if binding_row is None:
                raise KeyError(generation)
            binding = _goal_generation_from_row(binding_row)
            if binding.archived_at is not None:
                raise GoalProtocolConflict("Goal generation is archived")
            if not binding.goal_thread_id:
                raise GoalProtocolConflict("Goal generation has no goal thread")

            state_row = conn.execute(
                """SELECT state_json, state_version FROM agent_thread_state
                   WHERE thread_id = ?""",
                (binding.goal_thread_id,),
            ).fetchone()
            if state_row is None:
                raise GoalProtocolConflict("Goal thread state is missing")
            thread_state = AgentThreadState.model_validate_json(state_row["state_json"])
            goal_state = GoalState.model_validate(thread_state.context.get("goal_run") or {})
            if goal_state.generation != generation:
                raise GoalProtocolConflict("Goal state generation mismatch")
            if is_goal_terminal(thread_state.lifecycle):
                return None
            if goal_state.projected_sequence_number < 0:
                raise GoalProtocolConflict("Goal state has no projected INIT")

            projected_sequence = goal_state.projected_sequence_number
            next_sequence = projected_sequence + 1
            if goal_state.current_phase == "work":
                if next_sequence % 2 != 1:
                    raise GoalProtocolConflict("work phase has an invalid sequence")
                next_attempt = (next_sequence + 1) // 2
                next_phase = "work"
            elif goal_state.current_phase == "evaluator":
                if next_sequence % 2 != 0:
                    raise GoalProtocolConflict("evaluator phase has an invalid sequence")
                next_attempt = next_sequence // 2
                next_phase = "evaluator"
            else:
                raise GoalProtocolConflict("Goal state has an unknown phase")

            predecessor = conn.execute(
                """SELECT * FROM goal_protocol_records
                   WHERE generation = ? AND sequence_number = ?""",
                (generation, projected_sequence),
            ).fetchone()
            if predecessor is None or predecessor["status"] != "projected":
                raise GoalProtocolConflict("preceding Goal protocol record is not projected")

            spec = _goal_spec_for_outbox(conn, generation, thread_state)
            payload = {
                "phase": next_phase,
                "generation": generation,
                "attempt_number": next_attempt,
                "sequence_number": next_sequence,
                "spec": spec,
                "goal_state": goal_state.model_dump(mode="json"),
            }
            predecessor_payload = json.loads(predecessor["payload_json"] or "{}")
            if next_phase == "evaluator":
                if predecessor["phase"] != "checkpoint":
                    raise GoalProtocolConflict("evaluator phase lacks a checkpoint predecessor")
                payload["checkpoint"] = predecessor_payload
            elif projected_sequence > 0:
                if predecessor["phase"] != "decision":
                    raise GoalProtocolConflict("work phase lacks a decision predecessor")
                payload["decision"] = predecessor_payload

            _ensure_goal_successor_outbox(
                conn,
                generation=generation,
                thread_id=binding.goal_thread_id,
                phase=next_phase,
                sequence_number=next_sequence,
                payload=payload,
                expected_state_version=int(state_row["state_version"]),
                timestamp=now(),
            )
            row = conn.execute(
                """SELECT * FROM runtime_outbox
                   WHERE thread_id = ? AND kind = 'goal_prompt'
                     AND delivered_at IS NULL
                     AND payload_json LIKE ?
                   ORDER BY created_at DESC LIMIT 1""",
                (binding.goal_thread_id, f'%"sequence_number":{next_sequence}%'),
            ).fetchone()
            if row is None:
                rows = conn.execute(
                    """SELECT * FROM runtime_outbox
                       WHERE thread_id = ? AND kind = 'goal_prompt'
                       ORDER BY created_at DESC""",
                    (binding.goal_thread_id,),
                ).fetchall()
                for candidate in rows:
                    candidate_payload = json.loads(candidate["payload_json"] or "{}")
                    if (
                        candidate_payload.get("generation") == generation
                        and int(candidate_payload.get("sequence_number", -1)) == next_sequence
                    ):
                        row = candidate
                        break
            if row is None:
                raise GoalProtocolConflict("Goal phase outbox was not persisted")
            return _outbox_from_row(row)

        return await self._write(_tx)


    async def create_thread(
        self,
        thread: AgentThread,
        *,
        profile: ResolvedAgentProfile | RuntimeProfile,
        state: AgentThreadState | None = None,
        resource_scope: dict[str, Any] | None = None,
    ) -> LoadedThread:
        thread_state = state or AgentThreadState(
            thread_id=thread.thread_id, lifecycle=thread.lifecycle
        )
        if thread_state.thread_id != thread.thread_id:
            raise ValueError("thread state id must match thread id")
        scope = resource_scope or {}
        resolved_profile = _as_resolved_profile(profile)
        runtime_profile = resolved_profile.runtime_profile

        def _tx(conn):
            timestamp = now()
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
                    runtime_profile.profile_id,
                    runtime_profile.revision,
                    _json_profile(profile),
                    _json(scope),
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """INSERT INTO agent_thread_state (
                       thread_id, state_json, state_version, updated_at
                   ) VALUES (?, ?, 0, ?)""",
                (thread.thread_id, _json_model(thread_state), timestamp),
            )
            return LoadedThread(thread, resolved_profile, thread_state, 0, scope)

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
        resolved_profile = _resolved_profile_from_json(row["profile_json"])
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
            resolved_profile=resolved_profile,
            state=state,
            state_version=int(row["state_version"]),
            resource_scope=json.loads(row["resource_scope_json"] or "{}"),
        )

    async def rebind_thread_session(self, thread_id: str, session_id: str) -> None:
        def _tx(conn):
            timestamp = now()
            conn.execute(
                "UPDATE agent_threads SET session_id = ?, updated_at = ? WHERE id = ?",
                (session_id, timestamp, thread_id),
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
            timestamp = now()
            cur = conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(state), timestamp, thread_id, expected_state_version),
            )
            if cur.rowcount != 1:
                raise ThreadStateConflict("thread state_version conflict")
            conn.execute("UPDATE agent_threads SET updated_at = ? WHERE id = ?", (timestamp, thread_id))
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
                if (
                    existing["status"] == "prepared"
                    and not existing["side_effect_started"]
                    and float(existing["lease_expires_at"]) <= time.time()
                ):
                    timestamp = now()
                    next_token = int(existing["fencing_token"]) + 1
                    conn.execute(
                        """UPDATE runtime_turn_attempts
                           SET lease_owner = ?, fencing_token = ?,
                               lease_expires_at = ?, updated_at = ?
                           WHERE id = ? AND status = 'prepared'
                             AND side_effect_started = 0
                             AND fencing_token = ?
                             AND lease_expires_at <= ?""",
                        (
                            lease_owner,
                            next_token,
                            time.time() + lease_seconds,
                            timestamp,
                            existing["id"],
                            existing["fencing_token"],
                            time.time(),
                        ),
                    )
                    existing = conn.execute(
                        "SELECT * FROM runtime_turn_attempts WHERE id = ?",
                        (existing["id"],),
                    ).fetchone()
                return _attempt_from_row(existing)
            loaded = _loaded_from_conn(conn, thread_id)
            if loaded.state_version != expected_state_version:
                raise ThreadStateConflict("thread state_version conflict")
            timestamp = now()
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
                    timestamp,
                ),
            )
            running = loaded.state.model_copy(update={"lifecycle": LifecycleState.RUNNING})
            conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(running), timestamp, thread_id, expected_state_version),
            )
            return ThreadAttempt(
                attempt_id=attempt_id,
                thread_id=thread_id,
                source_outbox_id=source_outbox_id,
                state_version=expected_state_version + 1,
                fencing_token=fencing_token,
                lease_owner=lease_owner,
                status="prepared",
            )

        return await self._write(_tx)

    async def renew_attempt_lease(
        self,
        attempt_id: str,
        *,
        lease_owner: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> bool:
        def _tx(conn):
            expires_at = time.time() + lease_seconds
            timestamp = now()
            cur = conn.execute(
                """UPDATE runtime_turn_attempts
                   SET lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'prepared'
                     AND lease_owner = ? AND fencing_token = ?
                     AND lease_expires_at > ?""",
                (
                    expires_at,
                    timestamp,
                    attempt_id,
                    lease_owner,
                    fencing_token,
                    time.time(),
                ),
            )
            if cur.rowcount != 1:
                return False
            attempt = conn.execute(
                "SELECT source_outbox_id FROM runtime_turn_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            source_outbox = conn.execute(
                "SELECT id FROM runtime_outbox WHERE id = ?",
                (attempt["source_outbox_id"],),
            ).fetchone()
            if source_outbox is not None:
                outbox = conn.execute(
                    """UPDATE runtime_outbox
                       SET claimed_until = ?
                       WHERE id = ? AND delivered_at IS NULL AND claimed_by = ?""",
                    (expires_at, attempt["source_outbox_id"], lease_owner),
                )
                if outbox.rowcount != 1:
                    raise ThreadStateConflict("source outbox lease conflict")
            return True

        return await self._write(_tx)

    async def mark_side_effect_started(
        self,
        attempt_id: str,
        *,
        lease_owner: str,
        fencing_token: int,
    ) -> ThreadAttempt | None:
        def _tx(conn):
            timestamp = now()
            cur = conn.execute(
                """UPDATE runtime_turn_attempts
                   SET side_effect_started = 1, updated_at = ?
                   WHERE id = ? AND side_effect_started = 0 AND status = 'prepared'
                     AND lease_owner = ? AND fencing_token = ? AND lease_expires_at > ?""",
                (timestamp, attempt_id, lease_owner, fencing_token, time.time()),
            )
            row = conn.execute("SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            return _attempt_from_row(row) if cur.rowcount else None

        return await self._write(_tx)

    async def commit_decision(
        self,
        *,
        attempt_id: str,
        decision: RuntimeDecision,
        expected_state_version: int,
        lease_owner: str,
        fencing_token: int,
    ) -> CommitResult:
        commitnow = time.time()

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
            if (
                attempt["lease_owner"] != lease_owner
                or int(attempt["fencing_token"]) != int(fencing_token)
                or float(attempt["lease_expires_at"]) <= commitnow
            ):
                raise ThreadStateConflict("attempt lease conflict")
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
            goal_patch = decision.metadata.goal_state_patch if decision.metadata is not None else None
            updated_goal_state: dict[str, Any] | None = None
            if goal_patch is not None:
                from voidx.agent.domain.automation.goal import GoalState

                raw_goal_state = context.get("goal_run")
                if not isinstance(raw_goal_state, dict):
                    raise ValueError("goal state patch requires context['goal_run']")
                goal_state = GoalState.model_validate(raw_goal_state)
                patch = goal_patch.model_dump(exclude_none=True, mode="python")
                updated_goal_state = goal_state.model_copy(update=patch).model_dump(mode="json")
                context["goal_run"] = updated_goal_state
            next_state = loaded.state.model_copy(
                update={
                    "lifecycle": next_lifecycle,
                    "lifecycle_decision": decision,
                    "context": context,
                }
            )
            timestamp = now()
            next_version = expected_state_version + 1
            conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = ?, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(next_state), next_version, timestamp, thread_id, expected_state_version),
            )
            conn.execute(
                "UPDATE runtime_turn_attempts SET status = 'committed', updated_at = ? WHERE id = ?",
                (timestamp, attempt_id),
            )
            outbox_id = None
            if decision.outcome == "continue":
                outbox_id = _uid("wakeup")
                prior_frame = json.loads(attempt["input_frame_json"] or "{}")
                wakeup_payload = {
                    "decision": decision.model_dump(mode="json"),
                    **{
                        key: prior_frame[key]
                        for key in ("prompt", "display_text", "spec", "goal_state")
                        if key in prior_frame
                    },
                }
                if updated_goal_state is not None:
                    wakeup_payload["goal_state"] = updated_goal_state
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
                        timestamp,
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
            timestamp = now()
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
                    timestamp,
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
        self,
        *,
        lease_owner: str,
        lease_seconds: float,
        kind: str | None = None,
        thread_id_prefix: str | None = None,
        exclude_outbox_ids: set[str] | None = None,
    ) -> RuntimeOutboxItem | None:
        def _tx(conn):
            now_ts = time.time()
            clauses = [
                "delivered_at IS NULL",
                "available_at <= ?",
                "(claimed_until IS NULL OR claimed_until <= ?)",
            ]
            params: list[object] = [now_ts, now_ts]
            if kind is not None:
                clauses.append("kind = ?")
                params.append(kind)
            if thread_id_prefix is not None:
                clauses.append("thread_id LIKE ?")
                params.append(f"{thread_id_prefix}%")
            if exclude_outbox_ids:
                placeholders = ", ".join("?" for _ in exclude_outbox_ids)
                clauses.append(f"id NOT IN ({placeholders})")
                params.extend(exclude_outbox_ids)
            row = conn.execute(
                f"""SELECT * FROM runtime_outbox
                   WHERE {' AND '.join(clauses)}
                   ORDER BY available_at, created_at
                   LIMIT 1""",
                tuple(params),
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
                (now(), f"{prefix}%"),
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
                (now(), thread_id),
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
                (now(), attempt["source_outbox_id"]),
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

    async def set_needs_user_for_attempt(
        self,
        attempt_id: str,
        *,
        reason: str,
        lease_owner: str,
        fencing_token: int,
    ) -> LoadedThread:
        def _tx(conn):
            attempt = conn.execute(
                "SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            if (
                attempt["lease_owner"] != lease_owner
                or int(attempt["fencing_token"]) != int(fencing_token)
                or float(attempt["lease_expires_at"]) <= time.time()
            ):
                raise ThreadStateConflict("attempt lease conflict")
            loaded = _loaded_from_conn(conn, attempt["thread_id"])
            decision = RuntimeDecision(
                outcome="needs_user",
                summary="Recovery requires user review.",
                reason=reason,
            )
            state = loaded.state.model_copy(
                update={"lifecycle": LifecycleState.NEEDS_USER, "lifecycle_decision": decision}
            )
            timestamp = now()
            conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (_json_model(state), timestamp, loaded.thread.thread_id, loaded.state_version),
            )
            return _loaded_from_conn(conn, loaded.thread.thread_id)

        return await self._write(_tx)

    async def submit_goal_protocol(
        self,
        record: GoalProtocolRecord,
        *,
        attempt_id: str = "",
        lease_owner: str = "",
        fencing_token: int = 0,
    ) -> GoalProtocolRecord:
        if not isinstance(record, GoalProtocolRecord):
            raise TypeError("record must be a GoalProtocolRecord")

        def _tx(conn):
            _ensure_goal_generation_writable(conn, record.generation)
            binding = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (record.generation,),
            ).fetchone()
            if binding is not None and binding["archived_at"] is not None:
                raise GoalProtocolConflict("Goal generation is archived")
            if binding is not None and record.phase != "init":
                _validate_goal_attempt_lease(
                    conn,
                    record=record,
                    binding=binding,
                    attempt_id=attempt_id,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                )

            existing_by_id = conn.execute(
                "SELECT * FROM goal_protocol_records WHERE protocol_id = ?",
                (record.protocol_id,),
            ).fetchone()
            if existing_by_id is not None:
                existing = _goal_protocol_from_row(existing_by_id)
                if (
                    existing.generation == record.generation
                    and existing.sequence_number == record.sequence_number
                    and existing.payload_hash == record.payload_hash
                ):
                    return existing
                raise GoalProtocolConflict(
                    f"protocol id conflict: {record.protocol_id}"
                )

            existing_by_position = conn.execute(
                """SELECT * FROM goal_protocol_records
                   WHERE generation = ? AND sequence_number = ?""",
                (record.generation, record.sequence_number),
            ).fetchone()
            if existing_by_position is not None:
                existing = _goal_protocol_from_row(existing_by_position)
                if existing.payload_hash == record.payload_hash:
                    return existing
                raise GoalProtocolConflict(
                    "Goal protocol payload conflict at the same position"
                )

            if record.sequence_number > 0:
                previous = conn.execute(
                    """SELECT status FROM goal_protocol_records
                       WHERE generation = ? AND sequence_number = ?""",
                    (record.generation, record.sequence_number - 1),
                ).fetchone()
                if previous is None or previous["status"] != "projected":
                    raise GoalProtocolConflict(
                        "preceding Goal protocol record must be projected"
                    )

            conn.execute(
                """INSERT INTO goal_protocol_records (
                       protocol_id, parent_session_id, generation, phase,
                       attempt_number, sequence_number, turn_id, session_id,
                       payload_type, payload_json, status, payload_hash,
                       submitted_at, projected_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    record.protocol_id,
                    record.parent_session_id,
                    record.generation,
                    record.phase,
                    record.attempt_number,
                    record.sequence_number,
                    record.turn_id,
                    record.session_id,
                    record.payload_type,
                    _json(record.payload),
                    record.status,
                    record.payload_hash,
                    record.submitted_at.isoformat(),
                ),
            )
            return record

        return await self._write(_tx)

    async def commit_goal_phase(
        self,
        *,
        attempt_id: str,
        protocol_id: str,
        lease_owner: str,
        fencing_token: int,
        guidance_delivery_id: str = "",
    ) -> GoalProtocolRecord:
        def _tx(conn):
            row = conn.execute(
                "SELECT * FROM goal_protocol_records WHERE protocol_id = ?",
                (protocol_id,),
            ).fetchone()
            if row is None:
                raise KeyError(protocol_id)
            record = _goal_protocol_from_row(row)
            binding = conn.execute(
                "SELECT * FROM goal_generations WHERE generation = ?",
                (record.generation,),
            ).fetchone()
            if binding is None:
                raise GoalProtocolConflict("Goal generation binding is missing")
            attempt = _validate_goal_attempt_lease(
                conn,
                record=record,
                binding=binding,
                attempt_id=attempt_id,
                lease_owner=lease_owner,
                fencing_token=fencing_token,
            )
            projected = _project_goal_protocol_tx(
                conn,
                protocol_id,
                close_source_attempt=False,
            )
            timestamp = now()
            updated = conn.execute(
                """UPDATE runtime_turn_attempts
                   SET status = 'committed', updated_at = ?
                   WHERE id = ? AND status = 'prepared'
                     AND lease_owner = ? AND fencing_token = ?""",
                (timestamp, attempt_id, lease_owner, fencing_token),
            )
            if updated.rowcount != 1:
                raise ThreadStateConflict("attempt commit race")
            conn.execute(
                """UPDATE runtime_outbox
                   SET delivered_at = COALESCE(delivered_at, ?),
                       claimed_by = NULL, claimed_until = NULL
                   WHERE id = ?""",
                (timestamp, attempt["source_outbox_id"]),
            )
            if guidance_delivery_id:
                conn.execute(
                    """UPDATE guidance_inbox
                       SET consumed_at = COALESCE(consumed_at, ?)
                       WHERE delivery_id = ? AND consumed_at IS NULL""",
                    (timestamp, guidance_delivery_id),
                )
            return projected

        return await self._write(_tx)

    async def commit_goal_needs_resume(
        self,
        *,
        attempt_id: str,
        phase: str,
        reason: str,
        lease_owner: str,
        fencing_token: int,
        guidance_delivery_id: str = "",
    ) -> LoadedThread:
        if phase not in {"work", "evaluator"}:
            raise ValueError("invalid Goal phase")

        def _tx(conn):
            attempt = conn.execute(
                "SELECT * FROM runtime_turn_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            if (
                attempt["status"] != "prepared"
                or attempt["lease_owner"] != lease_owner
                or int(attempt["fencing_token"]) != int(fencing_token)
                or float(attempt["lease_expires_at"]) <= time.time()
            ):
                raise ThreadStateConflict("attempt lease conflict")
            frame = json.loads(attempt["input_frame_json"] or "{}")
            if frame.get("phase") != phase:
                raise GoalProtocolConflict("Goal attempt phase conflict")
            loaded = _loaded_from_conn(conn, attempt["thread_id"])
            goal_state = GoalState.model_validate(
                loaded.state.context.get("goal_run") or {}
            ).model_copy(
                update={
                    "current_phase": phase,
                    "phase_status": "needs_resume",
                    "interrupt_reason": reason,
                }
            )
            next_state = loaded.state.model_copy(
                update={
                    "lifecycle": LifecycleState.NEEDS_USER,
                    "lifecycle_decision": RuntimeDecision(
                        outcome="needs_resume",
                        summary="Goal phase requires durable resume.",
                        reason=reason,
                    ),
                    "context": {
                        **loaded.state.context,
                        "goal_run": goal_state.model_dump(mode="json"),
                    },
                }
            )
            timestamp = now()
            updated = conn.execute(
                """UPDATE agent_thread_state
                   SET state_json = ?, state_version = state_version + 1, updated_at = ?
                   WHERE thread_id = ? AND state_version = ?""",
                (
                    _json_model(next_state), timestamp,
                    loaded.thread.thread_id, loaded.state_version,
                ),
            )
            if updated.rowcount != 1:
                raise ThreadStateConflict("Goal state_version conflict")
            conn.execute(
                """UPDATE runtime_turn_attempts
                   SET status = 'committed', updated_at = ?
                   WHERE id = ? AND status = 'prepared'
                     AND lease_owner = ? AND fencing_token = ?""",
                (timestamp, attempt_id, lease_owner, fencing_token),
            )
            conn.execute(
                """UPDATE runtime_outbox
                   SET delivered_at = COALESCE(delivered_at, ?),
                       claimed_by = NULL, claimed_until = NULL
                   WHERE id = ?""",
                (timestamp, attempt["source_outbox_id"]),
            )
            if guidance_delivery_id:
                conn.execute(
                    """UPDATE guidance_inbox
                       SET delivery_id = NULL, delivered_phase = NULL
                       WHERE delivery_id = ? AND consumed_at IS NULL""",
                    (guidance_delivery_id,),
                )
            return _loaded_from_conn(conn, attempt["thread_id"])

        return await self._write(_tx)

    async def get_goal_protocol(self, protocol_id: str) -> GoalProtocolRecord | None:
        row = await self._one(
            "SELECT * FROM goal_protocol_records WHERE protocol_id = ?",
            (protocol_id,),
        )
        return _goal_protocol_from_row(row) if row is not None else None

    async def list_goal_protocols(self, generation: str) -> list[GoalProtocolRecord]:
        rows = await self._all(
            """SELECT * FROM goal_protocol_records
               WHERE generation = ? ORDER BY sequence_number""",
            (generation,),
        )
        return [_goal_protocol_from_row(row) for row in rows]

    async def project_goal_protocol(self, protocol_id: str) -> GoalProtocolRecord:
        """Project one durable Goal record and its phase successor atomically."""
        return await self._write(
            lambda conn: _project_goal_protocol_tx(conn, protocol_id)
        )

    async def ack_outbox(self, outbox_id: str) -> None:
        def _tx(conn):
            conn.execute(
                "UPDATE runtime_outbox SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?",
                (now(), outbox_id),
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


def _validate_goal_attempt_lease(
    conn,
    *,
    record: GoalProtocolRecord,
    binding,
    attempt_id: str,
    lease_owner: str,
    fencing_token: int,
):
    if not attempt_id or not lease_owner or fencing_token < 1:
        raise GoalProtocolConflict("Goal protocol attempt lease binding is missing")
    attempt = conn.execute(
        "SELECT * FROM runtime_turn_attempts WHERE id = ?",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        raise GoalProtocolConflict("Goal protocol attempt lease is missing")
    source = conn.execute(
        "SELECT * FROM runtime_outbox WHERE id = ?",
        (attempt["source_outbox_id"],),
    ).fetchone()
    now_ts = time.time()
    if (
        attempt["thread_id"] != binding["goal_thread_id"]
        or attempt["status"] != "prepared"
        or not bool(attempt["side_effect_started"])
        or attempt["lease_owner"] != lease_owner
        or int(attempt["fencing_token"]) != int(fencing_token)
        or float(attempt["lease_expires_at"]) <= now_ts
        or source is None
        or source["kind"] != "goal_prompt"
        or source["delivered_at"] is not None
        or source["claimed_by"] != lease_owner
        or float(source["claimed_until"] or 0) <= now_ts
    ):
        raise GoalProtocolConflict("Goal protocol attempt lease conflict")
    frame = json.loads(attempt["input_frame_json"] or "{}")
    expected_phase = {"checkpoint": "work", "decision": "evaluator"}.get(record.phase)
    expected_session = {
        "checkpoint": binding["work_session_id"],
        "decision": binding["evaluator_session_id"],
    }.get(record.phase)
    if (
        frame.get("generation") != record.generation
        or frame.get("phase") != expected_phase
        or int(frame.get("attempt_number", -1)) != record.attempt_number
        or record.parent_session_id != binding["main_session_id"]
        or record.session_id != expected_session
    ):
        raise GoalProtocolConflict("Goal protocol attempt binding conflict")
    return attempt


def _project_goal_protocol_tx(
    conn,
    protocol_id: str,
    *,
    close_source_attempt: bool = True,
) -> GoalProtocolRecord:
    row = conn.execute(
        "SELECT * FROM goal_protocol_records WHERE protocol_id = ?",
        (protocol_id,),
    ).fetchone()
    if row is None:
        raise KeyError(protocol_id)
    record = _goal_protocol_from_row(row)
    if record.status == "projected":
        return record

    binding_row = conn.execute(
        "SELECT * FROM goal_generations WHERE generation = ?",
        (record.generation,),
    ).fetchone()
    # The journal remains independently usable for intake/journal tests and
    # for an INIT that has not reached Boundary I yet. Once a generation is
    # bound, every phase projection must include its GoalState transition.
    if binding_row is None:
        projected_at = now()
        conn.execute(
            """UPDATE goal_protocol_records
               SET status = 'projected', projected_at = ?
               WHERE protocol_id = ? AND status = 'submitted'""",
            (projected_at, protocol_id),
        )
        updated = conn.execute(
            "SELECT * FROM goal_protocol_records WHERE protocol_id = ?",
            (protocol_id,),
        ).fetchone()
        return _goal_protocol_from_row(updated)

    _ensure_goal_generation_writable(conn, record.generation)
    binding = _goal_generation_from_row(binding_row)
    if not binding.goal_thread_id:
        raise GoalProtocolConflict("Goal generation has no goal thread")
    if record.parent_session_id != binding.main_session_id:
        raise GoalProtocolConflict("Goal protocol parent session mismatch")
    expected_session = {
        "checkpoint": binding.work_session_id,
        "decision": binding.evaluator_session_id,
    }.get(record.phase)
    if expected_session is not None and record.session_id != expected_session:
        raise GoalProtocolConflict("Goal protocol phase session mismatch")

    state_row = conn.execute(
        """SELECT state_json, state_version FROM agent_thread_state
           WHERE thread_id = ?""",
        (binding.goal_thread_id,),
    ).fetchone()
    if state_row is None:
        raise GoalProtocolConflict("Goal thread state is missing")
    thread_state = AgentThreadState.model_validate_json(state_row["state_json"])
    if is_goal_terminal(thread_state.lifecycle):
        raise GoalProtocolConflict("Goal generation is terminal")
    goal_state = GoalState.model_validate(
        thread_state.context.get("goal_run") or {}
    )
    if goal_state.generation != record.generation:
        raise GoalProtocolConflict("Goal state generation mismatch")
    expected_sequence = goal_state.projected_sequence_number + 1
    if record.sequence_number != expected_sequence:
        raise GoalProtocolConflict(
            "Goal projection must advance one sequence at a time"
        )
    if record.sequence_number > 0:
        previous = conn.execute(
            """SELECT status FROM goal_protocol_records
               WHERE generation = ? AND sequence_number = ?""",
            (record.generation, record.sequence_number - 1),
        ).fetchone()
        if previous is None or previous["status"] != "projected":
            raise GoalProtocolConflict(
                "preceding Goal protocol record must be projected"
            )

    payload = record.payload_model()
    timestamp = now()
    next_phase: str | None = None
    next_attempt: int | None = None
    next_sequence: int | None = None
    next_lifecycle = thread_state.lifecycle
    lifecycle_decision = thread_state.lifecycle_decision
    state_updates: dict[str, Any] = {
        "projected_sequence_number": record.sequence_number,
        "phase_status": "running",
        "last_protocol_id": record.protocol_id,
    }

    if record.phase == "checkpoint":
        if payload.generation != record.generation:
            raise GoalProtocolConflict("checkpoint generation mismatch")
        if payload.attempt_number != record.attempt_number:
            raise GoalProtocolConflict("checkpoint attempt mismatch")
        state_updates.update(
            {
                "current_phase": "evaluator",
                "last_work_checkpoint": payload.model_dump(mode="json"),
            }
        )
        next_phase = "evaluator"
        next_attempt = record.attempt_number
        next_sequence = record.sequence_number + 1
        next_lifecycle = LifecycleState.WAITING
        lifecycle_decision = RuntimeDecision(
            outcome="continue",
            summary="Work checkpoint projected.",
            reason="goal_checkpoint_projected",
        )
    elif record.phase == "decision":
        if payload.generation != record.generation:
            raise GoalProtocolConflict("decision generation mismatch")
        if payload.attempt_number != record.attempt_number:
            raise GoalProtocolConflict("decision attempt mismatch")
        is_continue = payload.status == "continue"
        state_updates.update(
            {
                "attempt_count": record.attempt_number,
                "current_phase": "work" if is_continue else "evaluator",
                "last_evaluator_summary": payload.summary,
                "last_evaluator_next_hint": payload.next_hint,
                "last_evaluator_missing": payload.missing_evidence,
                "blocked_reason": payload.reason if payload.status == "blocked" else "",
            }
        )
        if is_continue:
            next_phase = "work"
            next_attempt = record.attempt_number + 1
            next_sequence = record.sequence_number + 1
            next_lifecycle = LifecycleState.WAITING
            lifecycle_decision = RuntimeDecision(
                outcome="continue",
                summary=payload.summary,
                reason=payload.reason,
            )
        else:
            next_lifecycle = {
                "finished": LifecycleState.COMPLETED,
                "blocked": LifecycleState.BLOCKED,
            }[payload.status]
            lifecycle_decision = RuntimeDecision(
                outcome=("completed" if payload.status == "finished" else "blocked"),
                summary=payload.summary,
                reason=payload.reason,
            )
    else:
        raise GoalProtocolConflict(
            "Boundary projection only accepts checkpoint or decision"
        )

    projected_goal_state = goal_state.model_copy(update=state_updates)
    projected_thread_state = thread_state.model_copy(
        update={
            "lifecycle": next_lifecycle,
            "lifecycle_decision": lifecycle_decision,
            "context": {
                **thread_state.context,
                "goal_run": projected_goal_state.model_dump(mode="json"),
            },
        }
    )
    current_version = int(state_row["state_version"])
    next_version = current_version + 1
    updated_state = conn.execute(
        """UPDATE agent_thread_state
           SET state_json = ?, state_version = ?, updated_at = ?
           WHERE thread_id = ? AND state_version = ?""",
        (
            _json_model(projected_thread_state),
            next_version,
            timestamp,
            binding.goal_thread_id,
            current_version,
        ),
    )
    if updated_state.rowcount != 1:
        raise ThreadStateConflict("Goal state_version conflict")
    conn.execute(
        "UPDATE agent_threads SET updated_at = ? WHERE id = ?",
        (timestamp, binding.goal_thread_id),
    )
    projected = conn.execute(
        """UPDATE goal_protocol_records
           SET status = 'projected', projected_at = ?
           WHERE protocol_id = ? AND status = 'submitted'""",
        (timestamp, protocol_id),
    )
    if projected.rowcount != 1:
        raise GoalProtocolConflict("Goal protocol projection race")

    if next_phase is not None:
        spec = _goal_spec_for_outbox(conn, record.generation, thread_state)
        successor_payload = {
            "phase": next_phase,
            "generation": record.generation,
            "attempt_number": next_attempt,
            "sequence_number": next_sequence,
            "spec": spec,
            "goal_state": projected_goal_state.model_dump(mode="json"),
        }
        if record.phase == "checkpoint":
            successor_payload["checkpoint"] = payload.model_dump(mode="json")
        else:
            successor_payload["decision"] = payload.model_dump(mode="json")
        _ensure_goal_successor_outbox(
            conn,
            generation=record.generation,
            thread_id=binding.goal_thread_id,
            phase=next_phase,
            sequence_number=next_sequence,
            payload=successor_payload,
            expected_state_version=next_version,
            timestamp=timestamp,
        )
    elif record.phase == "decision":
        conn.execute(
            """UPDATE goal_generations SET terminal_at = COALESCE(terminal_at, ?)
               WHERE generation = ?""",
            (timestamp, record.generation),
        )
        public_summary = _public_summary_for_terminal_decision(
            generation=record.generation,
            thread_state=thread_state,
            goal_state=projected_goal_state,
            decision=payload,
            timestamp=timestamp,
        )
        _insert_goal_public_summary_tx(
            conn,
            generation=record.generation,
            main_session_id=binding.main_session_id,
            kind=public_summary.outcome,
            summary=public_summary,
        )

    if close_source_attempt and record.phase in {"checkpoint", "decision"}:
        _close_goal_source_attempt_tx(
            conn,
            record=record,
            goal_thread_id=binding.goal_thread_id,
            timestamp=timestamp,
        )

    updated = conn.execute(
        "SELECT * FROM goal_protocol_records WHERE protocol_id = ?",
        (protocol_id,),
    ).fetchone()
    return _goal_protocol_from_row(updated)



def _close_goal_source_attempt_tx(
    conn,
    *,
    record: GoalProtocolRecord,
    goal_thread_id: str,
    timestamp: str,
) -> None:
    expected_phase = {"checkpoint": "work", "decision": "evaluator"}[record.phase]
    candidates = conn.execute(
        """SELECT a.*, o.delivered_at
           FROM runtime_turn_attempts AS a
           JOIN runtime_outbox AS o ON o.id = a.source_outbox_id
           WHERE a.thread_id = ?
             AND a.status = 'prepared'
             AND a.side_effect_started = 1
             AND o.kind = 'goal_prompt'""",
        (goal_thread_id,),
    ).fetchall()
    matches = []
    for attempt in candidates:
        frame = json.loads(attempt["input_frame_json"] or "{}")
        if (
            frame.get("generation") == record.generation
            and frame.get("phase") == expected_phase
            and int(frame.get("attempt_number", -1)) == record.attempt_number
        ):
            matches.append(attempt)
    if len(matches) != 1:
        raise GoalProtocolConflict("Goal protocol source attempt is missing or ambiguous")
    attempt = matches[0]
    if attempt["delivered_at"] is not None:
        raise GoalProtocolConflict("Goal protocol source outbox is already delivered")
    updated = conn.execute(
        """UPDATE runtime_turn_attempts
           SET status = 'committed', updated_at = ?
           WHERE id = ? AND status = 'prepared' AND side_effect_started = 1""",
        (timestamp, attempt["id"]),
    )
    if updated.rowcount != 1:
        raise ThreadStateConflict("Goal source attempt commit race")
    conn.execute(
        """UPDATE runtime_outbox
           SET delivered_at = COALESCE(delivered_at, ?),
               claimed_by = NULL, claimed_until = NULL
           WHERE id = ?""",
        (timestamp, attempt["source_outbox_id"]),
    )
    conn.execute(
        """UPDATE guidance_inbox
           SET consumed_at = COALESCE(consumed_at, ?)
           WHERE delivery_id = ? AND consumed_at IS NULL""",
        (timestamp, f"attempt:{attempt['source_outbox_id']}"),
    )


def _cleanup_tombstone_for_target(
    conn,
    *,
    generation: str = "",
    thread_id: str = "",
    session_id: str = "",
):
    targets: list[str] = []
    params: list[str] = []
    if generation:
        targets.append("c.generation = ?")
        params.append(generation)
    if thread_id:
        targets.append(
            "EXISTS (SELECT 1 FROM goal_generations g "
            "WHERE g.generation = c.generation AND g.goal_thread_id = ?)"
        )
        params.append(thread_id)
    if session_id:
        targets.append("(c.work_session_id = ? OR c.evaluator_session_id = ?)")
        params.extend((session_id, session_id))
    if not targets:
        return None
    return conn.execute(
        """SELECT c.generation
           FROM goal_generation_cleanup c
           WHERE c.status IN ('pending', 'committed')
             AND ("""
        + " OR ".join(targets)
        + ") LIMIT 1",
        tuple(params),
    ).fetchone()


def _ensure_goal_generation_writable(conn, generation: str) -> None:
    if _cleanup_tombstone_for_target(conn, generation=generation) is not None:
        raise GoalProtocolConflict("Goal generation cleanup is in progress")


def _goal_protocol_from_row(row) -> GoalProtocolRecord:
    return GoalProtocolRecord.model_validate(
        {
            "protocol_id": row["protocol_id"],
            "parent_session_id": row["parent_session_id"],
            "generation": row["generation"],
            "phase": row["phase"],
            "attempt_number": row["attempt_number"],
            "sequence_number": row["sequence_number"],
            "turn_id": row["turn_id"],
            "session_id": row["session_id"],
            "payload_type": row["payload_type"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "status": row["status"],
            "payload_hash": row["payload_hash"],
            "submitted_at": row["submitted_at"],
            "projected_at": row["projected_at"],
        }
    )


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
        resolved_profile=_resolved_profile_from_json(row["profile_json"]),
        state=state,
        state_version=int(row["state_version"]),
        resource_scope=json.loads(row["resource_scope_json"] or "{}"),
    )


def _insert_goal_session(
    conn,
    *,
    session_id: str,
    workspace: str,
    profile_id: str,
    profile_snapshot: AgentProfileSnapshot,
    title: str,
) -> None:
    from voidx.llm.domain.model import DEFAULT_MODEL

    timestamp = now()
    conn.execute(
        """INSERT INTO sessions (
               id, title, workspace, directory, model_provider, model_name,
               runtime_profile, runtime_profile_revision,
               runtime_profile_content_hash, runtime_profile_hash,
               runtime_profile_source, runtime_profile_snapshot,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            title,
            workspace,
            workspace,
            "anthropic",
            DEFAULT_MODEL,
            profile_id,
            profile_snapshot.revision,
            profile_snapshot.content_hash,
            profile_snapshot.snapshot_hash,
            profile_snapshot.source,
            json.dumps(profile_snapshot.canonical_payload, sort_keys=True, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )




def _goal_spec_for_outbox(conn, generation: str, thread_state: AgentThreadState) -> dict[str, Any]:
    """Return the frozen GoalSpec fields used by every phase input frame."""
    init_row = conn.execute(
        """SELECT payload_json FROM goal_protocol_records
           WHERE generation = ? AND sequence_number = 0""",
        (generation,),
    ).fetchone()
    raw: dict[str, Any] = {}
    if init_row is not None:
        candidate = json.loads(init_row["payload_json"] or "{}")
        if isinstance(candidate, dict):
            raw = candidate
    if not raw:
        candidate = thread_state.context.get("goal_spec") or {}
        if isinstance(candidate, dict):
            raw = candidate
    keys = (
        "objective",
        "acceptance_condition",
        "achievement_method",
        "max_attempts",
        "workflow_enabled",
        "generation",
    )
    spec = {key: raw[key] for key in keys if key in raw}
    if spec.get("generation") != generation:
        raise GoalProtocolConflict("Goal outbox spec generation mismatch")
    if not spec.get("objective") or not spec.get("acceptance_condition"):
        raise GoalProtocolConflict("Goal outbox spec is incomplete")
    return spec


def _ensure_goal_successor_outbox(
    conn,
    *,
    generation: str,
    thread_id: str,
    phase: str,
    sequence_number: int,
    payload: dict[str, Any],
    expected_state_version: int,
    timestamp: str,
) -> None:
    """Insert exactly one phase outbox for a projected journal position."""
    rows = conn.execute(
        """SELECT * FROM runtime_outbox
           WHERE thread_id = ? AND kind = 'goal_prompt'""",
        (thread_id,),
    ).fetchall()
    for row in rows:
        existing_payload = json.loads(row["payload_json"] or "{}")
        if (
            existing_payload.get("generation") == generation
            and int(existing_payload.get("sequence_number", -1)) == sequence_number
        ):
            if existing_payload != payload:
                raise GoalProtocolConflict(
                    "Goal successor outbox payload conflict"
                )
            attempt = conn.execute(
                """SELECT 1 FROM runtime_turn_attempts
                   WHERE source_outbox_id = ? LIMIT 1""",
                (row["id"],),
            ).fetchone()
            if (
                row["delivered_at"] is not None
                and row["source_attempt_id"] is None
                and attempt is None
            ):
                conn.execute(
                    """UPDATE runtime_outbox
                       SET delivered_at = NULL, claimed_by = NULL, claimed_until = NULL
                       WHERE id = ?""",
                    (row["id"],),
                )
            return

    conn.execute(
        """INSERT INTO runtime_outbox (
               id, thread_id, kind, payload_json,
               expected_state_version, available_at, created_at
           ) VALUES (?, ?, 'goal_prompt', ?, ?, ?, ?)""",
        (
            _uid(f"goal-{phase}-{sequence_number}"),
            thread_id,
            _json(payload),
            expected_state_version,
            time.time(),
            timestamp,
        ),
    )


def _goal_runtime_failure_from_row(row) -> GoalRuntimeFailure:
    return GoalRuntimeFailure.model_validate(
        {
            "generation": row["generation"],
            "observed_sequence": row["observed_sequence"],
            "reason": row["reason"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "created_at": row["created_at"],
        }
    )


def _goal_public_summary_row(row) -> dict[str, Any]:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json") or "{}")
    return value


def _insert_goal_public_summary_tx(
    conn,
    *,
    generation: str,
    main_session_id: str,
    kind: str,
    summary: PublicSummary,
) -> None:
    payload = summary.model_dump(mode="json")
    conn.execute(
        """INSERT INTO goal_public_summary_outbox (
               summary_id, generation, main_session_id, kind, summary,
               payload_json, created_at, delivered_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            _uid("goal-summary"),
            generation,
            main_session_id,
            kind,
            summary.summary,
            _json(payload),
            payload["created_at"],
        ),
    )


def _goal_spec_from_thread_state(thread_state: AgentThreadState) -> GoalSpec:
    raw = thread_state.context.get("goal_spec") or {}
    return GoalSpec.model_validate(raw)


def _public_summary_for_terminal_decision(
    *,
    generation: str,
    thread_state: AgentThreadState,
    goal_state: GoalState,
    decision,
    timestamp: str,
) -> PublicSummary:
    outcome = "completed" if decision.status == "finished" else "blocked"
    return PublicSummary(
        generation=generation,
        phase="evaluator",
        outcome=outcome,
        objective_summary=_goal_spec_from_thread_state(thread_state).objective_summary(),
        attempt_count=goal_state.attempt_count,
        summary=decision.summary,
        created_at=timestamp,
    )


def _public_summary_for_failure(
    *,
    failure: GoalRuntimeFailure,
    thread_state: AgentThreadState,
    timestamp: str,
) -> PublicSummary:
    goal_state = GoalState.model_validate(thread_state.context.get("goal_run") or {})
    return PublicSummary(
        generation=failure.generation,
        phase="runtime",
        outcome="failed",
        objective_summary=_goal_spec_from_thread_state(thread_state).objective_summary(),
        attempt_count=goal_state.attempt_count,
        summary=f"Goal runtime failed: {failure.reason}",
        created_at=timestamp,
    )

def _guidance_from_row(row) -> Guidance:
    return Guidance(
        guidance_id=row["guidance_id"],
        text=row["text"],
        truncated=bool(row["truncated"]),
        source=row["source"],
        created_at=row["created_at"],
        target_session_id=row["target_session_id"],
        target_thread_id=row["target_thread_id"],
        target_run_id=row["target_run_id"],
        target_phase=row["target_phase"],
        delivery_id=row["delivery_id"],
        delivered_phase=row["delivered_phase"],
        consumed_at=row["consumed_at"],
    )


def _guidance_immutable_payload(guidance: Guidance) -> dict[str, Any]:
    return guidance.model_dump(
        mode="json",
        exclude={"delivery_id", "delivered_phase", "consumed_at"},
    )


def _guidance_matches(
    guidance: Guidance,
    *,
    session_id: str,
    thread_id: str,
    run_id: str,
    phase: str | None,
) -> bool:
    if guidance.target_session_id and guidance.target_session_id != session_id:
        return False
    if guidance.target_thread_id and guidance.target_thread_id != thread_id:
        return False
    if guidance.target_run_id and guidance.target_run_id != run_id:
        return False
    if guidance.target_phase and guidance.target_phase not in {"any", phase}:
        return False
    if guidance.target_session_id and not session_id:
        return False
    if guidance.target_thread_id and not thread_id:
        return False
    if guidance.target_run_id and not run_id:
        return False
    return True


def _binding_from_cleanup_tombstone(row) -> GoalGenerationBinding:
    completed_at = row["completed_at"] or row["requested_at"]
    return GoalGenerationBinding.model_validate(
        {
            "generation": row["generation"],
            "main_session_id": row["main_session_id"],
            "evaluator_session_id": row["evaluator_session_id"],
            "work_session_id": row["work_session_id"],
            "goal_thread_id": None,
            "visibility": "internal",
            "created_at": row["requested_at"],
            "terminal_at": completed_at,
            "archived_at": completed_at,
        }
    )


def _goal_generation_from_row(row) -> GoalGenerationBinding:
    return GoalGenerationBinding.model_validate(
        {
            "generation": row["generation"],
            "main_session_id": row["main_session_id"],
            "evaluator_session_id": row["evaluator_session_id"],
            "work_session_id": row["work_session_id"],
            "goal_thread_id": row["goal_thread_id"],
            "visibility": row["visibility"],
            "created_at": row["created_at"],
            "terminal_at": row["terminal_at"],
            "archived_at": row["archived_at"],
        }
    )


def _attempt_from_row(row) -> ThreadAttempt:
    return ThreadAttempt(
        attempt_id=row["id"],
        thread_id=row["thread_id"],
        source_outbox_id=row["source_outbox_id"],
        state_version=int(row["base_state_version"]) + 1,
        fencing_token=int(row["fencing_token"]),
        lease_owner=row["lease_owner"],
        status=row["status"],
        side_effect_started=bool(row["side_effect_started"]),
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_profile(value: ResolvedAgentProfile | RuntimeProfile) -> str:
    if isinstance(value, RuntimeProfile):
        payload = value.model_dump(mode="json")
    else:
        payload = {
            "snapshot": value.snapshot.model_dump(mode="json"),
            "runtime_profile": value.runtime_profile.model_dump(mode="json"),
        }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _resolved_profile_from_json(payload: str) -> ResolvedAgentProfile:
    from voidx.agent.application.agent_profile_snapshot import restore_from_snapshot

    raw = json.loads(payload)
    snapshot = raw.get("snapshot") if isinstance(raw, dict) else None
    persisted_runtime = raw.get("runtime_profile") if isinstance(raw, dict) else None
    if isinstance(snapshot, dict):
        resolved = restore_from_snapshot(AgentProfileSnapshot.model_validate(snapshot))
        if isinstance(persisted_runtime, dict):
            runtime_profile = _runtime_profile_from_json(json.dumps(persisted_runtime))
            return resolved.model_copy(update={"runtime_profile": runtime_profile})
        return resolved
    return _legacy_resolved_profile(_runtime_profile_from_json(payload))


def _runtime_profile_from_json(payload: str) -> RuntimeProfile:
    from voidx.agent.domain.prompt_policy import revive_prompt_policy

    profile = RuntimeProfile.model_validate_json(payload)
    revived = revive_prompt_policy(profile.profile_id, profile.prompt_policy)
    if revived is None and profile.prompt_policy is None:
        return profile
    return profile.model_copy(update={"prompt_policy": revived})


def _as_resolved_profile(
    profile: ResolvedAgentProfile | RuntimeProfile,
) -> ResolvedAgentProfile:
    if isinstance(profile, ResolvedAgentProfile):
        return profile
    return _legacy_resolved_profile(profile)


def _legacy_resolved_profile(profile: RuntimeProfile) -> ResolvedAgentProfile:
    run_mode = {"goal": "goal_eval", "loop": "loop_dynamic"}.get(
        profile.protocol, "single"
    )
    payload = {
        "name": profile.profile_id,
        "revision": profile.revision,
        "display_name": profile.name,
        "prompt_policy": _prompt_policy_id(profile),
        "run_mode": run_mode,
        "hitl_mode": "interactive",
        "identity": profile.system_prompt,
        "extra_rules": list(profile.constraints),
        "persona": profile.persona,
    }
    content_hash = content_hash_of(payload)
    snapshot = AgentProfileSnapshot(
        profile_id=profile.profile_id,
        revision=profile.revision,
        source="project",
        content_hash=content_hash,
        snapshot_hash=content_hash_of({
            "source": "project",
            "profile_id": profile.profile_id,
            "revision": profile.revision,
            "content_hash": content_hash,
        }),
        canonical_payload=payload,
    )
    return ResolvedAgentProfile(
        snapshot=snapshot,
        runtime_profile=profile,
        workflow_context=None,
        run_config=resolve_run_config(run_mode),
        resource_policy=ResourcePolicy(),
    )


def _prompt_policy_id(profile: RuntimeProfile) -> str:
    name = type(profile.prompt_policy).__name__.removesuffix("PromptPolicy").lower()
    return name or "coding"


def _json_model(value) -> str:
    return value.model_dump_json()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
