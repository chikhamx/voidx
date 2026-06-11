"""Structured runtime state persistence for session resume."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.runtime import (
    InteractionMode,
    PendingApproval,
    TaskIntent,
    TaskPhase,
    TaskRun,
    TaskRunStatus,
    TaskState,
)
from voidx.memory.store import _execute_commit, _fetch_one, _now, _write_transaction
from voidx.workflow.runtime import WorkflowRunState


class RuntimeStateSnapshot(BaseModel):
    interaction_mode: InteractionMode = InteractionMode.AUTO
    task_state: TaskState = Field(default_factory=TaskState)
    task_run: TaskRun = Field(default_factory=TaskRun)
    compaction_summary: str = ""


class MessageRuntimeSnapshot(BaseModel):
    message_id: int
    session_id: str
    interaction_mode: InteractionMode = InteractionMode.AUTO
    task_intent: TaskIntent = TaskIntent.CHAT
    intent_resolution_reason: str = ""
    goal: str = ""
    goal_phase: str = TaskPhase.CLARIFY.value
    goal_status: str = TaskRunStatus.IDLE.value
    goal_turn_count: int = 0
    pending_approval: PendingApproval | None = None
    intent_confidence: float | None = None
    intent_source: str = ""
    intent_refined: bool = False
    available_tool_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)


async def save_runtime_state(session_id: str, snapshot: RuntimeStateSnapshot) -> None:
    await save_session_runtime_state(
        session_id,
        snapshot.interaction_mode,
        snapshot.task_state,
        snapshot.compaction_summary,
    )
    await save_session_task_run(session_id, snapshot.task_run)


async def load_runtime_state(session_id: str) -> RuntimeStateSnapshot:
    return RuntimeStateSnapshot(
        interaction_mode=await load_interaction_mode(session_id),
        task_state=await load_task_state(session_id),
        task_run=await load_task_run(session_id),
        compaction_summary=await load_compaction_summary(session_id),
    )


async def save_session_runtime_state(
    session_id: str,
    interaction_mode: InteractionMode,
    task_state: TaskState,
    compaction_summary: str = "",
) -> None:
    await _execute_commit(
        """INSERT INTO session_runtime_state (
               session_id, interaction_mode, current_intent, previous_intent,
               current_goal, awaiting_implementation_approval, approved_scope,
               pending_approval_json, last_plan_summary, recent_user_texts_json,
               compaction_summary, updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               interaction_mode = excluded.interaction_mode,
               current_intent = excluded.current_intent,
               previous_intent = excluded.previous_intent,
               current_goal = excluded.current_goal,
               awaiting_implementation_approval = excluded.awaiting_implementation_approval,
               approved_scope = excluded.approved_scope,
               pending_approval_json = excluded.pending_approval_json,
               last_plan_summary = excluded.last_plan_summary,
               recent_user_texts_json = excluded.recent_user_texts_json,
               compaction_summary = excluded.compaction_summary,
               updated_at = excluded.updated_at""",
        (
            session_id,
            interaction_mode.value,
            task_state.current_intent.value,
            task_state.previous_intent.value if task_state.previous_intent else None,
            task_state.current_goal,
            1 if task_state.pending_approval else 0,
            task_state.pending_approval.scope if task_state.pending_approval else "",
            _dump_pending_approval(task_state.pending_approval),
            task_state.last_plan_summary,
            _dump_string_list(task_state.recent_user_texts),
            compaction_summary,
            _now(),
        ),
    )


async def load_interaction_mode(session_id: str) -> InteractionMode:
    row = await _fetch_one(
        "SELECT interaction_mode FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return InteractionMode.AUTO
    return InteractionMode.parse(row["interaction_mode"])


async def load_task_state(session_id: str) -> TaskState:
    row = await _fetch_one(
        "SELECT * FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return TaskState()
    pending = _load_pending_approval(
        row["pending_approval_json"] if "pending_approval_json" in row.keys() else "",
        awaiting=bool(row["awaiting_implementation_approval"]),
        scope=row["approved_scope"],
    )
    return TaskState(
        current_intent=TaskIntent(row["current_intent"]),
        previous_intent=TaskIntent(row["previous_intent"]) if row["previous_intent"] else None,
        current_goal=row["current_goal"],
        pending_approval=pending,
        last_plan_summary=row["last_plan_summary"],
        recent_user_texts=_load_string_list(
            row["recent_user_texts_json"] if "recent_user_texts_json" in row.keys() else ""
        )[-2:],
    )


async def load_compaction_summary(session_id: str) -> str:
    row = await _fetch_one(
        "SELECT compaction_summary FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return ""
    return row["compaction_summary"] or ""


async def save_session_task_run(session_id: str, task_run: TaskRun) -> None:
    await _execute_commit(
        """INSERT INTO session_task_runs (
               session_id, goal, phase, status, approved_scope,
               awaiting_implementation_approval, pending_approval_json, turn_count, workflow_runs_json,
               updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               goal = excluded.goal,
               phase = excluded.phase,
               status = excluded.status,
               approved_scope = excluded.approved_scope,
               awaiting_implementation_approval = excluded.awaiting_implementation_approval,
               pending_approval_json = excluded.pending_approval_json,
               turn_count = excluded.turn_count,
               workflow_runs_json = excluded.workflow_runs_json,
               updated_at = excluded.updated_at""",
        (
            session_id,
            task_run.goal,
            task_run.phase.value,
            task_run.status.value,
            task_run.pending_approval.scope if task_run.pending_approval else "",
            1 if task_run.pending_approval else 0,
            _dump_pending_approval(task_run.pending_approval),
            task_run.turn_count,
            _dump_workflow_runs(task_run.workflow_runs),
            _now(),
        ),
    )


async def load_task_run(session_id: str) -> TaskRun:
    row = await _fetch_one(
        "SELECT * FROM session_task_runs WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return TaskRun()
    pending = _load_pending_approval(
        row["pending_approval_json"] if "pending_approval_json" in row.keys() else "",
        awaiting=bool(row["awaiting_implementation_approval"]),
        scope=row["approved_scope"],
    )
    return TaskRun(
        goal=row["goal"],
        phase=TaskPhase(row["phase"]),
        status=TaskRunStatus(row["status"]),
        pending_approval=pending,
        turn_count=row["turn_count"],
        workflow_runs=_load_workflow_runs(row["workflow_runs_json"] if "workflow_runs_json" in row.keys() else ""),
    )


def _dump_workflow_runs(workflow_runs: dict[str, WorkflowRunState]) -> str:
    return json.dumps(
        {name: run.model_dump(mode="json") for name, run in workflow_runs.items()},
        ensure_ascii=False,
    )


def _dump_pending_approval(pending: PendingApproval | None) -> str:
    if pending is None:
        return ""
    return json.dumps(pending.model_dump(mode="json"), ensure_ascii=False)


def _dump_string_list(items: list[str]) -> str:
    return json.dumps([item for item in items if isinstance(item, str)][-2:], ensure_ascii=False)


def _load_pending_approval(
    raw: str,
    *,
    awaiting: bool = False,
    scope: str = "",
) -> PendingApproval | None:
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            try:
                return PendingApproval.model_validate(data)
            except ValueError:
                return None
    if awaiting and scope:
        return PendingApproval(scope=scope)
    return None


def _load_workflow_runs(raw: str) -> dict[str, WorkflowRunState]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    runs: dict[str, WorkflowRunState] = {}
    for name, value in data.items():
        try:
            run = WorkflowRunState.model_validate(value)
        except ValueError:
            continue
        runs[str(name)] = run
    return runs


async def save_message_runtime_snapshot(snapshot: MessageRuntimeSnapshot) -> None:
    legacy_pending = snapshot.pending_approval
    await _execute_commit(
        """INSERT INTO message_runtime_snapshots (
               message_id, session_id, interaction_mode, task_intent,
               implementation_allowed, intent_resolution_reason, goal, goal_phase,
               goal_status, goal_turn_count, awaiting_implementation_approval,
               approved_scope, pending_approval_json, intent_confidence, intent_source, intent_refined,
               available_tool_ids_json, created_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO UPDATE SET
               interaction_mode = excluded.interaction_mode,
               task_intent = excluded.task_intent,
               implementation_allowed = excluded.implementation_allowed,
               intent_resolution_reason = excluded.intent_resolution_reason,
               goal = excluded.goal,
               goal_phase = excluded.goal_phase,
               goal_status = excluded.goal_status,
               goal_turn_count = excluded.goal_turn_count,
               awaiting_implementation_approval = excluded.awaiting_implementation_approval,
               approved_scope = excluded.approved_scope,
               pending_approval_json = excluded.pending_approval_json,
               intent_confidence = excluded.intent_confidence,
               intent_source = excluded.intent_source,
               intent_refined = excluded.intent_refined,
               available_tool_ids_json = excluded.available_tool_ids_json""",
        (
            snapshot.message_id,
            snapshot.session_id,
            snapshot.interaction_mode.value,
            snapshot.task_intent.value,
            1 if snapshot.task_intent == TaskIntent.IMPLEMENT else 0,
            snapshot.intent_resolution_reason,
            snapshot.goal,
            snapshot.goal_phase,
            snapshot.goal_status,
            snapshot.goal_turn_count,
            1 if legacy_pending else 0,
            legacy_pending.scope if legacy_pending else "",
            _dump_pending_approval(legacy_pending),
            snapshot.intent_confidence,
            snapshot.intent_source,
            1 if snapshot.intent_refined else 0,
            json.dumps(snapshot.available_tool_ids, ensure_ascii=False),
            snapshot.created_at,
        ),
    )


async def load_message_runtime_snapshot(message_id: int) -> MessageRuntimeSnapshot | None:
    row = await _fetch_one(
        "SELECT * FROM message_runtime_snapshots WHERE message_id = ?",
        (message_id,),
    )
    if not row:
        return None
    pending = _load_pending_approval(
        row["pending_approval_json"] if "pending_approval_json" in row.keys() else "",
        awaiting=bool(row["awaiting_implementation_approval"]),
        scope=row["approved_scope"],
    )
    return MessageRuntimeSnapshot(
        message_id=row["message_id"],
        session_id=row["session_id"],
        interaction_mode=InteractionMode(row["interaction_mode"]),
        task_intent=TaskIntent(row["task_intent"]),
        intent_resolution_reason=row["intent_resolution_reason"],
        goal=row["goal"],
        goal_phase=row["goal_phase"],
        goal_status=row["goal_status"],
        goal_turn_count=row["goal_turn_count"],
        pending_approval=pending,
        intent_confidence=row["intent_confidence"] if "intent_confidence" in row.keys() else None,
        intent_source=row["intent_source"] if "intent_source" in row.keys() else "",
        intent_refined=bool(row["intent_refined"]) if "intent_refined" in row.keys() else False,
        available_tool_ids=_load_string_list(
            row["available_tool_ids_json"] if "available_tool_ids_json" in row.keys() else ""
        ),
        created_at=row["created_at"],
    )


def _load_string_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str)]


async def clear_runtime_state(session_id: str) -> None:
    def _run(conn):
        conn.execute("DELETE FROM session_runtime_state WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_task_runs WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM message_runtime_snapshots WHERE session_id = ?", (session_id,))

    await _write_transaction(_run)
