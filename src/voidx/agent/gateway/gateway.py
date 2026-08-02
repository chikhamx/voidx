from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from voidx.agent.gateway.models import (
    USER_MESSAGE_TYPES,
    AgentMessage,
    UserMessageType,
    AgentRun,
    AgentRunStatus,
)


class AgentGatewayError(ValueError):
    pass


_TERMINAL_STATUSES: set[AgentRunStatus] = {"completed", "failed", "cancelled"}


@dataclass
class _RunRecord:
    run: AgentRun
    inbox: asyncio.Queue[AgentMessage]
    done: asyncio.Event
    task: asyncio.Task[None] | None = None
    terminal_sent: bool = False


class AgentGateway:
    def __init__(self, *, inbox_capacity: int = 100, max_payload_bytes: int = 65536) -> None:
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
                if current is not None and current.run.status not in _TERMINAL_STATUSES:
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
            self._validate_route(source, target, operation="send")
            self._validate_send_open(source, target)
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
            await self._put_message(target, message)
            if message_type == "result":
                await self._finish(
                    sender_run_id,
                    status="completed",
                    result=payload,
                )
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
        self._validate_route(requester, target, operation="control")
        if timeout == 0:
            await target.done.wait()
        else:
            try:
                await asyncio.wait_for(target.done.wait(), timeout=timeout)
            except TimeoutError:
                return self._copy_run(target.run)
        return self._copy_run(target.run)

    def get_run(self, *, requester_run_id: str, target_run_id: str) -> AgentRun:
        requester = self._require_run(requester_run_id)
        target = self._require_run(target_run_id)
        self._validate_route(requester, target, operation="control")
        return self._copy_run(target.run)

    async def cancel(self, *, requester_run_id: str, target_run_id: str) -> AgentRun:
        requester = self._require_run(requester_run_id)
        target = self._require_run(target_run_id)
        self._validate_route(requester, target, operation="control")
        if target.run.status not in _TERMINAL_STATUSES:
            if target.task is not None:
                target.task.cancel()
                try:
                    await target.task
                except asyncio.CancelledError:
                    pass
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

    def get_parent_run_id(self, run_id: str) -> str | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        parent_run_id = record.run.parent_run_id
        return parent_run_id or None

    def list_runs(self, *, session_id: str | None = None) -> list[AgentRun]:
        return [
            self._copy_run(record.run)
            for record in self._runs.values()
            if session_id is None or record.run.session_id == session_id
        ]

    async def _close_records(self, records: list[_RunRecord]) -> None:
        for record in records:
            if record.run.status not in _TERMINAL_STATUSES and record.task is not None:
                record.task.cancel()
        for record in records:
            if record.task is not None:
                try:
                    await record.task
                except asyncio.CancelledError:
                    pass
        for record in records:
            self._runs.pop(record.run.run_id, None)

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
        if record is None or record.run.status in _TERMINAL_STATUSES:
            return
        result_payload = self._result_payload(result) if result is not None else None
        record.run.status = status
        record.run.result = result_payload
        record.run.error = error
        record.run.updated_at = time.time()
        record.done.set()
        if send_lifecycle and not record.terminal_sent and record.run.parent_run_id:
            record.terminal_sent = True
            parent = self._runs.get(record.run.parent_run_id)
            if parent is not None:
                payload: dict[str, Any] = {"run_id": run_id}
                if error is not None:
                    payload["error"] = error
                message = AgentMessage(
                    message_id=f"msg_{uuid.uuid4().hex}",
                    session_id=record.run.session_id,
                    source_run_id=run_id,
                    target_run_id=parent.run.run_id,
                    type=status,
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


    def _validate_send_open(self, source: _RunRecord, target: _RunRecord) -> None:
        if source.run.status in _TERMINAL_STATUSES:
            raise AgentGatewayError("Source run is terminal")
        if target.run.status in _TERMINAL_STATUSES:
            raise AgentGatewayError("Target run is terminal")

    def _validate_route(self, source: _RunRecord, target: _RunRecord, *, operation: str) -> None:
        if source.run.session_id != target.run.session_id:
            raise AgentGatewayError("Runs must belong to the same session")
        if operation == "send":
            if source.run.agent_type == "root" and target.run.parent_run_id == source.run.run_id:
                return
            if target.run.run_id == source.run.parent_run_id:
                return
            raise AgentGatewayError("Route not allowed")
        if operation == "control":
            if source.run.agent_type == "root" and target.run.parent_run_id == source.run.run_id:
                return
            raise AgentGatewayError("Route not allowed")
        raise AgentGatewayError(f"Unknown operation: {operation}")

    def _require_run(self, run_id: str) -> _RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise AgentGatewayError(f"Unknown run: {run_id}")
        return record

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except TypeError as exc:
            raise AgentGatewayError(f"Payload is not JSON serializable: {exc}") from exc
        if len(encoded) > self._max_payload_bytes:
            raise AgentGatewayError("Payload is too large")

    @staticmethod
    def _result_payload(value: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {"result": value}

    @staticmethod
    def _new_run_id() -> str:
        return f"run_{uuid.uuid4().hex}"

    @staticmethod
    def _copy_run(run: AgentRun) -> AgentRun:
        return run.model_copy(deep=True)
