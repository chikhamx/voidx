from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CANCEL_ACK_TIMEOUT = 5.0


from voidx.agent.domain.subagent import (
    USER_MESSAGE_TYPES,
    TERMINAL_STATUSES,
    AgentActivity,
    AgentActivityCategory,
    AgentGatewayError,
    AgentMessage,
    AgentProgress,
    UserMessageType,
    AgentRun,
    AgentRunStatus,
    AgentToolActivity,
    ensure_control_route,
    ensure_open_send,
    ensure_send_route,
    finish_run,
)




@dataclass(frozen=True)
class _FileImpact:
    read_paths: frozenset[str] = frozenset()
    edited_paths: frozenset[str] = frozenset()


@dataclass
class _RunRecord:
    run: AgentRun
    inbox: asyncio.Queue[AgentMessage]
    done: asyncio.Event
    task: asyncio.Task[None] | None = None
    terminal_sent: bool = False
    seen_activity_ids: set[str] = field(default_factory=set)
    finished_activity_ids: set[str] = field(default_factory=set)
    active_activities: dict[str, AgentActivity] = field(default_factory=dict)
    pending_file_impacts: dict[str, _FileImpact] = field(default_factory=dict)
    read_paths: set[str] = field(default_factory=set)
    edited_paths: set[str] = field(default_factory=set)


class InProcessSubagentGateway:
    def __init__(self, *, inbox_capacity: int = 256, max_payload_bytes: int = 65536) -> None:
        self._inbox_capacity = inbox_capacity
        self._max_payload_bytes = max_payload_bytes
        self._runs: dict[str, _RunRecord] = {}
        self._root_by_session: dict[str, str] = {}
        self._send_lock = asyncio.Lock()

    def ensure_root(self, session_id: str) -> str:
        existing = self._root_by_session.get(session_id)
        if existing is not None and existing in self._runs:
            return existing
        now = time.time()
        run_id = f"root:{session_id}"
        run = AgentRun(
            run_id=run_id,
            session_id=session_id,
            parent_run_id="",
            agent_type="root",
            agent_name="root",
            description="Root agent",
            status="running",
            created_at=now,
            updated_at=now,
            current_activity=_idle_activity(now),
            last_activity_at=now,
        )
        self._runs[run_id] = _RunRecord(
            run=run,
            inbox=asyncio.Queue(maxsize=self._inbox_capacity),
            done=asyncio.Event(),
        )
        self._root_by_session[session_id] = run_id
        return run_id

    async def spawn(
        self,
        *,
        session_id: str,
        parent_run_id: str,
        agent_name: str,
        description: str,
        runner: Callable[[str], Awaitable[str | dict[str, Any]]],
    ) -> AgentRun:
        parent = self._require_run(parent_run_id)
        if parent.run.session_id != session_id:
            raise AgentGatewayError("Parent run must belong to the same session")
        run_id = self._new_run_id()
        now = time.time()
        run = AgentRun(
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            agent_type="sub",
            agent_name=agent_name,
            description=description,
            status="running",
            created_at=now,
            updated_at=now,
            current_activity=_idle_activity(now),
            last_activity_at=now,
        )
        record = _RunRecord(
            run=run,
            inbox=asyncio.Queue(maxsize=self._inbox_capacity),
            done=asyncio.Event(),
        )
        self._runs[run_id] = record

        async def _run() -> None:
            try:
                result = await runner(run_id)
            except asyncio.CancelledError:
                await self._finish(run_id, status="cancelled")
                raise
            except Exception as exc:
                await self._finish(run_id, status="failed", error=str(exc)[:500])
            else:
                current = self._runs.get(run_id)
                if current is not None and current.run.status not in TERMINAL_STATUSES:
                    await self._finish(run_id, status="completed", result=result)

        record.task = asyncio.create_task(_run())
        return self._copy_run(record.run)

    async def send(
        self,
        *,
        sender_run_id: str,
        target_run_id: str,
        message_type: UserMessageType,
        payload: dict[str, Any],
    ) -> AgentMessage:
        if message_type not in USER_MESSAGE_TYPES:
            raise AgentGatewayError(
                f"Lifecycle message type '{message_type}' is gateway-internal and cannot be sent"
            )
        async with self._send_lock:
            source = self._require_run(sender_run_id)
            target = self._require_run(target_run_id)
            ensure_send_route(source.run, target.run)
            ensure_open_send(source.run, target.run)
            self._validate_payload(payload)
            message = AgentMessage(
                message_id=f"msg_{uuid.uuid4().hex}",
                session_id=source.run.session_id,
                source_run_id=sender_run_id,
                target_run_id=target_run_id,
                type=message_type,
                payload=payload,
                created_at=time.time(),
            )
            if message_type == "result":
                await self._finish(
                    sender_run_id,
                    status="completed",
                    result=payload,
                    send_lifecycle=False,
                )
                await self._put_message(target, message, lifecycle=True)
                await self._send_lifecycle(source)
            else:
                await self._put_message(target, message)
            return message

    async def receive(self, *, run_id: str, limit: int = 1, timeout: float = 0) -> list[AgentMessage]:
        if limit <= 0:
            raise AgentGatewayError("limit must be greater than 0")
        record = self._require_run(run_id)
        messages: list[AgentMessage] = []
        first = await self._get_one(record, timeout=timeout)
        if first is None:
            return []
        messages.append(first)
        while len(messages) < limit:
            try:
                messages.append(record.inbox.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def wait(self, *, requester_run_id: str, target_run_id: str, timeout: float) -> AgentRun:
        if timeout < 0:
            raise AgentGatewayError("timeout must be greater than or equal to 0")
        requester = self._require_run(requester_run_id)
        target = self._require_run(target_run_id)
        ensure_control_route(requester.run, target.run)
        if target.run.status in TERMINAL_STATUSES:
            return self._copy_run(target.run, wait_outcome="already_terminal")
        if timeout == 0:
            await target.done.wait()
            return self._copy_run(target.run, wait_outcome="terminal_reached_during_wait")
        try:
            await asyncio.wait_for(target.done.wait(), timeout=timeout)
        except TimeoutError:
            if target.run.status in TERMINAL_STATUSES:
                return self._copy_run(target.run, wait_outcome="terminal_reached_during_wait")
            return self._copy_run(target.run, wait_outcome="timed_out")
        return self._copy_run(target.run, wait_outcome="terminal_reached_during_wait")

    def get_run(self, *, requester_run_id: str, target_run_id: str) -> AgentRun:
        requester = self._require_run(requester_run_id)
        target = self._require_run(target_run_id)
        ensure_control_route(requester.run, target.run)
        return self._copy_run(target.run)

    async def cancel(self, *, requester_run_id: str, target_run_id: str) -> AgentRun:
        requester = self._require_run(requester_run_id)
        target = self._require_run(target_run_id)
        ensure_control_route(requester.run, target.run)
        await self._reap_tasks([target])
        if target.run.status not in TERMINAL_STATUSES:
            await self._finish(target_run_id, status="cancelled")
        return self._copy_run(target.run)

    async def close_session(self, session_id: str) -> None:
        records = [record for record in self._runs.values() if record.run.session_id == session_id]
        await self._close_records(records)
        self._root_by_session.pop(session_id, None)

    async def close_all(self) -> None:
        records = list(self._runs.values())
        await self._close_records(records)
        self._root_by_session.clear()

    def lookup_run(self, run_id: str) -> AgentRun | None:
        record = self._runs.get(run_id)
        return self._copy_run(record.run) if record is not None else None

    def get_parent_run_id(self, run_id: str) -> str | None:
        run = self.lookup_run(run_id)
        return (run.parent_run_id or None) if run is not None else None

    def list_runs(self, *, session_id: str | None = None) -> list[AgentRun]:
        return [
            self._copy_run(record.run)
            for record in self._runs.values()
            if session_id is None or record.run.session_id == session_id
        ]
    def list_child_runs(self, parent_run_id: str) -> list[AgentRun]:
        return [
            self._copy_run(record.run)
            for record in self._runs.values()
            if record.run.parent_run_id == parent_run_id
        ]

    def start_model_activity(self, run_id: str, *, activity_id: str) -> None:
        self._start_activity(run_id, activity_id=activity_id, category="thinking")

    def touch_model_activity(self, run_id: str, *, activity_id: str) -> None:
        record = self._require_run(run_id)
        activity = record.active_activities.get(activity_id)
        if activity is None or record.run.status in TERMINAL_STATUSES:
            return
        now = time.time()
        record.active_activities[activity_id] = activity.model_copy(
            update={"last_observed_at": now}
        )
        self._update_activity_snapshot(record, now=now)

    def finish_model_activity(
        self,
        run_id: str,
        *,
        activity_id: str,
        succeeded: bool,
    ) -> None:
        self._finish_activity(run_id, activity_id=activity_id, succeeded=succeeded)

    def start_tool_activity(
        self,
        run_id: str,
        *,
        tool_name: str,
        tool_call_id: str,
        args: dict | None = None,
        workspace: str = "",
    ) -> None:
        record = self._require_run(run_id)
        if record.run.status in TERMINAL_STATUSES:
            return
        now = time.time()
        is_new = tool_call_id not in record.seen_activity_ids
        if is_new:
            self._start_activity(
                run_id,
                activity_id=tool_call_id,
                category=_tool_activity_category(tool_name),
                now=now,
            )
            record.pending_file_impacts[tool_call_id] = _file_impact(
                tool_name,
                args or {},
                workspace=workspace,
            )
            record.run = record.run.model_copy(
                update={"progress": _progress_after_tool_start(record.run.progress, tool_name)}
            )
        else:
            return
        activity = AgentToolActivity(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status="running",
            started_at=now,
        )
        active_tools = [
            item for item in record.run.active_tools
            if item.tool_call_id != tool_call_id
        ]
        active_tools.append(activity)
        record.run = record.run.model_copy(update={
            "active_tools": active_tools,
            "updated_at": now,
        })

    def finish_tool_activity(self, run_id: str, *, tool_call_id: str, succeeded: bool) -> None:
        record = self._require_run(run_id)
        if tool_call_id in record.finished_activity_ids:
            return
        active_tools = list(record.run.active_tools)
        tool_activity = next(
            (item for item in active_tools if item.tool_call_id == tool_call_id),
            None,
        )
        abstract_activity = record.active_activities.get(tool_call_id)
        if tool_activity is None and abstract_activity is None:
            return
        now = time.time()
        active_tools = [
            item for item in active_tools
            if item.tool_call_id != tool_call_id
        ]
        last_tool = (
            tool_activity.model_copy(update={
                "status": "succeeded" if succeeded else "failed",
                "finished_at": now,
            })
            if tool_activity is not None
            else record.run.last_tool
        )
        if succeeded:
            impact = record.pending_file_impacts.get(tool_call_id, _FileImpact())
            record.read_paths.update(impact.read_paths)
            record.edited_paths.update(impact.edited_paths)
        record.pending_file_impacts.pop(tool_call_id, None)
        self._finish_activity(
            run_id,
            activity_id=tool_call_id,
            succeeded=succeeded,
            now=now,
        )
        record.run = record.run.model_copy(update={
            "active_tools": active_tools,
            "last_tool": last_tool,
            "progress": record.run.progress.model_copy(update={
                "files_read": len(record.read_paths),
                "files_edited": len(record.edited_paths),
            }),
            "updated_at": now,
        })

    def _start_activity(
        self,
        run_id: str,
        *,
        activity_id: str,
        category: AgentActivityCategory,
        now: float | None = None,
    ) -> None:
        record = self._require_run(run_id)
        if (
            not activity_id
            or record.run.status in TERMINAL_STATUSES
            or activity_id in record.seen_activity_ids
        ):
            return
        observed_at = time.time() if now is None else now
        record.seen_activity_ids.add(activity_id)
        record.active_activities[activity_id] = AgentActivity(
            category=category,
            status="running",
            started_at=observed_at,
            last_observed_at=observed_at,
        )
        self._update_activity_snapshot(record, now=observed_at)

    def _finish_activity(
        self,
        run_id: str,
        *,
        activity_id: str,
        succeeded: bool,
        now: float | None = None,
    ) -> None:
        record = self._require_run(run_id)
        if activity_id in record.finished_activity_ids:
            return
        activity = record.active_activities.pop(activity_id, None)
        if activity is None:
            return
        observed_at = time.time() if now is None else now
        record.finished_activity_ids.add(activity_id)
        record.run = record.run.model_copy(update={
            "recent_activity": activity.model_copy(update={
                "status": "succeeded" if succeeded else "failed",
                "last_observed_at": observed_at,
                "finished_at": observed_at,
            })
        })
        self._update_activity_snapshot(record, now=observed_at)

    @staticmethod
    def _update_activity_snapshot(record: _RunRecord, *, now: float) -> None:
        current = (
            max(
                record.active_activities.values(),
                key=lambda activity: (activity.last_observed_at, activity.started_at),
            )
            if record.active_activities
            else _idle_activity(now)
        )
        record.run = record.run.model_copy(update={
            "current_activity": current,
            "last_activity_at": now,
            "updated_at": now,
        })


    async def _close_records(self, records: list[_RunRecord]) -> None:
        await self._reap_tasks(records)
        for record in records:
            self._runs.pop(record.run.run_id, None)

    async def _reap_tasks(self, records: list[_RunRecord]) -> None:
        tasks = {
            record.task
            for record in records
            if record.task is not None and not record.task.done()
        }
        if not tasks:
            return
        current_task = asyncio.current_task()
        if current_task in tasks:
            raise AgentGatewayError(
                "A child runner cannot reap its own task",
                reason="self_reap",
            )
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=_CANCEL_ACK_TIMEOUT)
        if pending:
            raise AgentGatewayError(
                "Child cancellation was not acknowledged",
                reason="cancel_timeout",
            )
        await asyncio.gather(*done, return_exceptions=True)

    async def _finish(
        self,
        run_id: str,
        *,
        status: AgentRunStatus,
        result: dict[str, Any] | str | None = None,
        error: str | None = None,
        send_lifecycle: bool = True,
    ) -> None:
        record = self._runs.get(run_id)
        if record is None or record.run.status in TERMINAL_STATUSES:
            return
        now = time.time()
        record.active_activities.clear()
        record.pending_file_impacts.clear()
        record.run = finish_run(
            record.run,
            status=status,
            result=result,
            error=error,
            now=now,
        ).model_copy(update={
            "active_tools": [],
            "current_activity": None,
            "last_activity_at": now,
        })
        record.done.set()
        if send_lifecycle:
            await self._send_lifecycle(record)

    async def _send_lifecycle(self, record: _RunRecord) -> None:
        if record.terminal_sent or not record.run.parent_run_id:
            return
        record.terminal_sent = True
        parent = self._runs.get(record.run.parent_run_id)
        if parent is None:
            return
        payload: dict[str, Any] = {"run_id": record.run.run_id}
        if record.run.error is not None:
            payload["error"] = record.run.error
        message = AgentMessage(
            message_id=f"msg_{uuid.uuid4().hex}",
            session_id=record.run.session_id,
            source_run_id=record.run.run_id,
            target_run_id=parent.run.run_id,
            type=record.run.status,
            payload=payload,
            created_at=time.time(),
        )
        await self._put_message(parent, message, lifecycle=True)

    async def _get_one(self, record: _RunRecord, *, timeout: float) -> AgentMessage | None:
        if timeout <= 0:
            try:
                return record.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(record.inbox.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def _put_message(self, record: _RunRecord, message: AgentMessage, *, lifecycle: bool = False) -> None:
        if record.inbox.full():
            if lifecycle:
                try:
                    record.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            else:
                raise AgentGatewayError("Inbox is full")
        await record.inbox.put(message)



    def _require_run(self, run_id: str) -> _RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise AgentGatewayError(
                f"Unknown run: {run_id}",
                reason="unknown_run",
            )
        return record

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except TypeError as exc:
            raise AgentGatewayError(f"Payload is not JSON serializable: {exc}") from exc
        if len(encoded) > self._max_payload_bytes:
            raise AgentGatewayError("Payload is too large")


    @staticmethod
    def _copy_run(run: AgentRun, *, wait_outcome=None) -> AgentRun:
        return run.model_copy(deep=True, update={"wait_outcome": wait_outcome})

    @staticmethod
    def _new_run_id() -> str:
        return f"run_{uuid.uuid4().hex}"


def _idle_activity(observed_at: float) -> AgentActivity:
    return AgentActivity(
        category="other",
        status="running",
        started_at=observed_at,
        last_observed_at=observed_at,
    )


def _tool_activity_category(tool_name: str) -> AgentActivityCategory:
    if tool_name == "read":
        return "reading"
    if tool_name in {"write", "replace", "manage"}:
        return "editing"
    if tool_name == "bash":
        return "running_command"
    if tool_name in {"find", "search", "lsp", "websearch", "webfetch"}:
        return "searching"
    return "other"


def _progress_after_tool_start(progress: AgentProgress, tool_name: str) -> AgentProgress:
    if tool_name == "bash":
        return progress.model_copy(update={"commands_run": progress.commands_run + 1})
    if tool_name in {"find", "search", "lsp", "websearch", "webfetch"}:
        return progress.model_copy(update={"searches": progress.searches + 1})
    if tool_name not in {"read", "write", "replace", "manage"}:
        return progress.model_copy(update={"other_actions": progress.other_actions + 1})
    return progress


def _file_impact(tool_name: str, args: dict, *, workspace: str) -> _FileImpact:
    if tool_name == "read":
        path = _normalized_path(args.get("file_path"), workspace=workspace)
        return _FileImpact(read_paths=frozenset({path}) if path else frozenset())
    if tool_name in {"write", "replace"}:
        path = _normalized_path(args.get("file_path"), workspace=workspace)
        return _FileImpact(edited_paths=frozenset({path}) if path else frozenset())
    if tool_name != "manage" or args.get("kind", "file") != "file":
        return _FileImpact()
    op = str(args.get("op") or "")
    if op in {"create", "delete"}:
        values = args.get("paths")
        paths = values if isinstance(values, list) else [values]
    elif op == "move":
        moves = args.get("moves") or []
        paths = [move.get("dest") for move in moves if isinstance(move, dict)]
    else:
        paths = []
    normalized = {
        path
        for value in paths
        if (path := _normalized_path(value, workspace=workspace))
    }
    return _FileImpact(edited_paths=frozenset(normalized))


def _normalized_path(value: object, *, workspace: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(workspace or ".") / path
    return str(path.resolve(strict=False))
