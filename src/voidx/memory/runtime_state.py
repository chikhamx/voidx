"""Structured runtime state persistence for session resume."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

import voidx.memory.store as store
from voidx.memory.jsonl_store import append_session_record, read_session_records
from voidx.memory.store import _execute_commit, _fetch_one, _now, _write_transaction
from voidx.runtime import (
    GoalSpec,
    InteractionMode,
    TaskIntent,
    TaskState,
    TodoRunState,
    WorkflowRoute,
)
from voidx.workflow.types import WorkflowRunState


class RuntimeStateSnapshot(BaseModel):
    interaction_mode: InteractionMode = InteractionMode.AUTO
    task_state: TaskState = Field(default_factory=TaskState)
    compaction_summary: str = ""
    session_time: str = ""


class MessageRuntimeSnapshot(BaseModel):
    message_id: int
    session_id: str
    interaction_mode: InteractionMode = InteractionMode.AUTO
    task_intent: TaskIntent = TaskIntent.CODING
    current_goal: GoalSpec | None = None
    workflow_route: WorkflowRoute | None = None
    workflow_runs: dict[str, WorkflowRunState] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


async def save_runtime_state(session_id: str, snapshot: RuntimeStateSnapshot) -> None:
    await save_session_runtime_state(
        session_id,
        snapshot.interaction_mode,
        snapshot.task_state,
        snapshot.compaction_summary,
        snapshot.session_time,
    )


async def load_runtime_state(session_id: str) -> RuntimeStateSnapshot:
    interaction_mode = await load_interaction_mode(session_id)
    task_state, session_time = await load_task_state_with_session_time(session_id)
    return RuntimeStateSnapshot(
        interaction_mode=interaction_mode,
        task_state=task_state,
        compaction_summary=await load_compaction_summary(session_id),
        session_time=session_time,
    )


async def save_session_runtime_state(
    session_id: str,
    interaction_mode: InteractionMode,
    task_state: TaskState,
    compaction_summary: str = "",
    session_time: str = "",
) -> None:
    await _execute_commit(
        """INSERT INTO session_runtime_state (
               session_id, interaction_mode, current_intent, previous_intent,
               current_goal_json, workflow_route_json, workflow_runs_json,
               recent_user_texts_json, todo_state_json, compaction_summary,
               session_time, updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               interaction_mode = excluded.interaction_mode,
               current_intent = excluded.current_intent,
               previous_intent = excluded.previous_intent,
               current_goal_json = excluded.current_goal_json,
               workflow_route_json = excluded.workflow_route_json,
               workflow_runs_json = excluded.workflow_runs_json,
               recent_user_texts_json = excluded.recent_user_texts_json,
               todo_state_json = excluded.todo_state_json,
               compaction_summary = excluded.compaction_summary,
               session_time = excluded.session_time,
               updated_at = excluded.updated_at""",
        (
            session_id,
            interaction_mode.value,
            task_state.current_intent.value,
            task_state.previous_intent.value if task_state.previous_intent else None,
            _dump_goal(task_state.current_goal),
            _dump_workflow_route(task_state.workflow_route),
            _dump_workflow_runs(task_state.workflow_runs),
            _dump_string_list(task_state.recent_user_texts),
            _dump_todo_state(task_state.todo_state),
            compaction_summary,
            session_time,
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
    task_state, _session_time = await load_task_state_with_session_time(session_id)
    return task_state


async def load_task_state_with_session_time(session_id: str) -> tuple[TaskState, str]:
    row = await _fetch_one(
        "SELECT * FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return TaskState(), ""
    return (
        TaskState(
            current_intent=TaskIntent(row["current_intent"]),
            previous_intent=TaskIntent(row["previous_intent"]) if row["previous_intent"] else None,
            current_goal=_load_goal(row["current_goal_json"]),
            workflow_route=_load_workflow_route(row["workflow_route_json"]),
            workflow_runs=_load_workflow_runs(row["workflow_runs_json"]),
            recent_user_texts=_load_string_list(row["recent_user_texts_json"])[-2:],
            todo_state=_load_todo_state(row["todo_state_json"]),
        ),
        row["session_time"] or "",
    )


async def load_compaction_summary(session_id: str) -> str:
    row = await _fetch_one(
        "SELECT compaction_summary FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return ""
    return row["compaction_summary"] or ""


def _dump_workflow_runs(workflow_runs: dict[str, WorkflowRunState]) -> str:
    return json.dumps(
        {name: run.model_dump(mode="json") for name, run in workflow_runs.items()},
        ensure_ascii=False,
    )


def _dump_goal(goal: GoalSpec | None) -> str:
    if goal is None:
        return ""
    return json.dumps(goal.model_dump(mode="json"), ensure_ascii=False)


def _dump_workflow_route(route: WorkflowRoute | None) -> str:
    if route is None or not (route.join or route.leave):
        return ""
    return json.dumps(route.model_dump(mode="json"), ensure_ascii=False)


def _dump_todo_state(todo_state: TodoRunState | None) -> str:
    if todo_state is None or not todo_state.items:
        return ""
    return json.dumps(todo_state.model_dump(mode="json"), ensure_ascii=False)


def _dump_string_list(items: list[str]) -> str:
    return json.dumps([item for item in items if isinstance(item, str)][-2:], ensure_ascii=False)


def _load_goal(raw: str) -> GoalSpec | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        return GoalSpec.model_validate(data)
    except ValueError:
        return None


def _load_workflow_route(raw: str) -> WorkflowRoute | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        route = WorkflowRoute.model_validate(data)
    except ValueError:
        return None
    return route if route.join or route.leave else None


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


def _load_todo_state(raw: str) -> TodoRunState | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        state = TodoRunState.model_validate(data)
    except ValueError:
        return None
    return state if state.items else None


async def save_message_runtime_snapshot(snapshot: MessageRuntimeSnapshot) -> None:
    await _execute_commit(
        """INSERT INTO session_runtime_state (
               session_id, interaction_mode, current_intent,
               current_goal_json, workflow_route_json, workflow_runs_json,
               session_time, updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               interaction_mode = excluded.interaction_mode,
               current_intent = excluded.current_intent,
               current_goal_json = excluded.current_goal_json,
               workflow_route_json = excluded.workflow_route_json,
               workflow_runs_json = excluded.workflow_runs_json,
               updated_at = excluded.updated_at""",
        (
            snapshot.session_id,
            snapshot.interaction_mode.value,
            snapshot.task_intent.value,
            _dump_goal(snapshot.current_goal),
            _dump_workflow_route(snapshot.workflow_route),
            _dump_workflow_runs(snapshot.workflow_runs),
            "",
            snapshot.created_at,
        ),
    )
    await append_session_record(
        snapshot.session_id,
        "runtime_debug.jsonl",
        _message_runtime_snapshot_record(snapshot),
    )


async def load_message_runtime_snapshot(message_id: int, *, session_id: str | None = None) -> MessageRuntimeSnapshot | None:
    return await _load_message_runtime_snapshot_jsonl(message_id, session_id=session_id)


def _message_runtime_snapshot_record(snapshot: MessageRuntimeSnapshot) -> dict:
    return {
        "type": "message_runtime_snapshot",
        "message_id": snapshot.message_id,
        "session_id": snapshot.session_id,
        "interaction_mode": snapshot.interaction_mode.value,
        "task_intent": snapshot.task_intent.value,
        "current_goal": _dump_goal(snapshot.current_goal),
        "workflow_route": _dump_workflow_route(snapshot.workflow_route),
        "workflow_runs": _dump_workflow_runs(snapshot.workflow_runs),
        "created_at": snapshot.created_at,
    }


async def _load_message_runtime_snapshot_jsonl(message_id: int, *, session_id: str | None = None) -> MessageRuntimeSnapshot | None:
    sessions_dir = store.DATA_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    if session_id is not None:
        candidate_dirs = [sessions_dir / session_id]
    else:
        candidate_dirs = [p for p in sessions_dir.iterdir() if p.is_dir()]
    for session_path in candidate_dirs:
        sid = session_path.name
        runtime_debug_path = session_path / "runtime_debug.jsonl"
        if not runtime_debug_path.exists():
            continue
        if await _runtime_snapshot_deleted(sid, message_id):
            continue
        records = await read_session_records(sid, "runtime_debug.jsonl") or []
        for record in reversed(records):
            if record.get("type") != "message_runtime_snapshot":
                continue
            if int(record.get("message_id") or -1) != message_id:
                continue
            return _message_runtime_snapshot_from_record(record)
    return None


def _message_runtime_snapshot_from_record(record: dict) -> MessageRuntimeSnapshot | None:
    try:
        return MessageRuntimeSnapshot(
            message_id=int(record["message_id"]),
            session_id=str(record["session_id"]),
            interaction_mode=InteractionMode(str(record["interaction_mode"])),
            task_intent=TaskIntent(str(record["task_intent"])),
            current_goal=_load_goal(str(record.get("current_goal") or "")),
            workflow_route=_load_workflow_route(str(record.get("workflow_route") or "")),
            workflow_runs=_load_workflow_runs(str(record.get("workflow_runs") or "")),
            created_at=str(record.get("created_at") or _now()),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _runtime_snapshot_deleted(session_id: str, message_id: int) -> bool:
    records = await read_session_records(session_id, "runtime.jsonl") or []
    for record in records:
        if record.get("type") != "runtime_state_deleted":
            continue
        mode = record.get("mode")
        if mode == "all":
            return True
        if mode == "from" and message_id >= int(record.get("first_message_id") or 0):
            return True
        if mode == "through" and message_id <= int(record.get("last_message_id") or -1):
            return True
        if mode == "message" and message_id == int(record.get("message_id") or -1):
            return True
    return False


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

    await _write_transaction(_run)
