"""Structured runtime state persistence for session resume."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import TaskPhase, TaskRun, TaskRunStatus, TaskState
from voidx.memory.session import _now
from voidx.memory.store import _execute_commit, _fetch_one


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
    implementation_allowed: bool = False
    intent_resolution_reason: str = ""
    goal: str = ""
    goal_phase: str = TaskPhase.CLARIFY.value
    goal_status: str = TaskRunStatus.IDLE.value
    goal_turn_count: int = 0
    awaiting_implementation_approval: bool = False
    approved_scope: str = ""
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
               last_plan_summary, compaction_summary, updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               interaction_mode = excluded.interaction_mode,
               current_intent = excluded.current_intent,
               previous_intent = excluded.previous_intent,
               current_goal = excluded.current_goal,
               awaiting_implementation_approval = excluded.awaiting_implementation_approval,
               approved_scope = excluded.approved_scope,
               last_plan_summary = excluded.last_plan_summary,
               compaction_summary = excluded.compaction_summary,
               updated_at = excluded.updated_at""",
        (
            session_id,
            interaction_mode.value,
            task_state.current_intent.value,
            task_state.previous_intent.value if task_state.previous_intent else None,
            task_state.current_goal,
            1 if task_state.awaiting_implementation_approval else 0,
            task_state.approved_scope,
            task_state.last_plan_summary,
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
    return TaskState(
        current_intent=TaskIntent(row["current_intent"]),
        previous_intent=TaskIntent(row["previous_intent"]) if row["previous_intent"] else None,
        current_goal=row["current_goal"],
        awaiting_implementation_approval=bool(row["awaiting_implementation_approval"]),
        approved_scope=row["approved_scope"],
        last_plan_summary=row["last_plan_summary"],
    )


async def load_compaction_summary(session_id: str) -> str:
    row = await _fetch_one(
        "SELECT compaction_summary FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return ""
    return row["compaction_summary"] if "compaction_summary" in row.keys() else ""


async def save_session_task_run(session_id: str, task_run: TaskRun) -> None:
    await _execute_commit(
        """INSERT INTO session_task_runs (
               session_id, goal, phase, status, approved_scope,
               awaiting_implementation_approval, turn_count, updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               goal = excluded.goal,
               phase = excluded.phase,
               status = excluded.status,
               approved_scope = excluded.approved_scope,
               awaiting_implementation_approval = excluded.awaiting_implementation_approval,
               turn_count = excluded.turn_count,
               updated_at = excluded.updated_at""",
        (
            session_id,
            task_run.goal,
            task_run.phase.value,
            task_run.status.value,
            task_run.approved_scope,
            1 if task_run.awaiting_implementation_approval else 0,
            task_run.turn_count,
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
    return TaskRun(
        goal=row["goal"],
        phase=TaskPhase(row["phase"]),
        status=TaskRunStatus(row["status"]),
        approved_scope=row["approved_scope"],
        awaiting_implementation_approval=bool(row["awaiting_implementation_approval"]),
        turn_count=row["turn_count"],
    )


async def save_message_runtime_snapshot(snapshot: MessageRuntimeSnapshot) -> None:
    await _execute_commit(
        """INSERT INTO message_runtime_snapshots (
               message_id, session_id, interaction_mode, task_intent,
               implementation_allowed, intent_resolution_reason, goal, goal_phase,
               goal_status, goal_turn_count, awaiting_implementation_approval,
               approved_scope, created_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
               approved_scope = excluded.approved_scope""",
        (
            snapshot.message_id,
            snapshot.session_id,
            snapshot.interaction_mode.value,
            snapshot.task_intent.value,
            1 if snapshot.implementation_allowed else 0,
            snapshot.intent_resolution_reason,
            snapshot.goal,
            snapshot.goal_phase,
            snapshot.goal_status,
            snapshot.goal_turn_count,
            1 if snapshot.awaiting_implementation_approval else 0,
            snapshot.approved_scope,
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
    return MessageRuntimeSnapshot(
        message_id=row["message_id"],
        session_id=row["session_id"],
        interaction_mode=InteractionMode(row["interaction_mode"]),
        task_intent=TaskIntent(row["task_intent"]),
        implementation_allowed=bool(row["implementation_allowed"]),
        intent_resolution_reason=row["intent_resolution_reason"],
        goal=row["goal"],
        goal_phase=row["goal_phase"],
        goal_status=row["goal_status"],
        goal_turn_count=row["goal_turn_count"],
        awaiting_implementation_approval=bool(row["awaiting_implementation_approval"]),
        approved_scope=row["approved_scope"],
        created_at=row["created_at"],
    )


async def clear_runtime_state(session_id: str) -> None:
    await _execute_commit("DELETE FROM session_runtime_state WHERE session_id = ?", (session_id,))
    await _execute_commit("DELETE FROM session_task_runs WHERE session_id = ?", (session_id,))
    await _execute_commit("DELETE FROM message_runtime_snapshots WHERE session_id = ?", (session_id,))
