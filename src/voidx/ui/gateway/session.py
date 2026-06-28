"""v2 JSON-RPC gateway session for web/desktop frontends.

Replaces v1 envelope broadcasting with:
- WorkspaceSnapshot on connect (v2 model)
- UiEventItemAdapter for event → Item notification conversion
- MethodDispatch for JSON-RPC request handling
- Multi-thread routing (each thread has its own adapter)
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol

import uuid

from voidx.ui.gateway.adapter import UiEventItemAdapter
from voidx.ui.gateway.diff_review import DiffReviewSession
from voidx.ui.gateway.terminal import TerminalManager, TerminalSession
from voidx.ui.output.events.schema import UiEvent
from voidx.ui.output.tree import OutputTree
from voidx.ui.protocol import (
    UiCommand,
    UiRequest,
    UiResponse,
    tree_to_snapshot,
)
from voidx.ui.protocol.v2.envelope import (
    ERR_TURN_IN_PROGRESS,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
)
from voidx.ui.protocol.v2.methods import MethodDispatch, MethodParamsError
from voidx.ui.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.ui.protocol.v2.threads import ThreadInfo


class ProtocolClient(Protocol):
    async def send_text(self, text: str) -> None:
        """Send an encoded protocol envelope to the connected client."""


class GatewaySession:
    """v2 JSON-RPC gateway session with multi-thread support."""

    def __init__(
        self,
        tree_provider: Callable[[], OutputTree],
        *,
        thread_id: str = "",
        session_id: str = "",
        command_handler: Callable[[UiCommand], Awaitable[None] | None] | None = None,
    ) -> None:
        self._tree_provider = tree_provider
        self._session_id = session_id or thread_id
        self._command_handler = command_handler
        self._clients: set[ProtocolClient] = set()
        self._pending_requests: dict[str, asyncio.Future[UiResponse]] = {}
        self._seq = 0
        self._thread_id_provider: Callable[[], str] | None = None

        # Multi-thread state
        self._threads: dict[str, ThreadInfo] = {}
        self._adapters: dict[str, UiEventItemAdapter] = {}
        self._active_thread_id = thread_id or ""

        if thread_id:
            self._threads[thread_id] = ThreadInfo(thread_id=thread_id)
            self._adapters[thread_id] = UiEventItemAdapter(
                thread_id=thread_id, turn_id="",
            )

        # v2 method dispatch
        self.methods = MethodDispatch()
        self.terminal_manager = TerminalManager()
        self._diff_reviews: dict[str, DiffReviewSession] = {}
        self._register_default_methods()

    # ── properties ────────────────────────────────────────────────────────

    @property
    def clients(self) -> frozenset[ProtocolClient]:
        return frozenset(self._clients)

    @property
    def active_thread_id(self) -> str:
        return self._active_thread_id

    # ── client connection ─────────────────────────────────────────────────

    async def connect(self, client: ProtocolClient) -> None:
        self._clients.add(client)
        try:
            await client.send_text(self._encode_snapshot())
        except Exception:
            self._clients.discard(client)
            raise

    def disconnect(self, client: ProtocolClient) -> None:
        self._clients.discard(client)

    # ── v1 compatibility (for run_loop.py registration) ───────────────────

    def set_command_handler(
        self,
        handler: Callable[[UiCommand], Awaitable[None] | None] | None,
    ) -> None:
        self._command_handler = handler

    def set_thread_id_provider(self, provider: Callable[[], str]) -> None:
        self._thread_id_provider = provider

    async def handle_command(self, command: UiCommand) -> None:
        if self._command_handler is None:
            return
        result = self._command_handler(command)
        if inspect.isawaitable(result):
            await result

    async def request(self, request: UiRequest) -> UiResponse | None:
        if not self._clients:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[UiResponse] = loop.create_future()
        self._pending_requests[request.request_id] = future
        notification = JsonRpcNotification(
            method="ui.request",
            params=request.model_dump(),
        )
        try:
            await self._broadcast(notification.model_dump_json())
            if not self._clients:
                return None
            return await future
        finally:
            self._pending_requests.pop(request.request_id, None)

    async def handle_response(self, response: UiResponse) -> None:
        future = self._pending_requests.pop(response.request_id, None)
        if future is not None and not future.done():
            future.set_result(response)

    # ── v2 event broadcasting ─────────────────────────────────────────────

    async def broadcast_event(self, event: UiEvent, *, thread_id: str = "") -> None:
        if not self._clients:
            return
        tid = thread_id or self._active_thread_id
        if not tid and self._thread_id_provider is not None:
            tid = self._thread_id_provider() or ""
        if not tid:
            return
        adapter = self._adapters.get(tid)
        if adapter is None:
            if tid not in self._threads:
                self._threads[tid] = ThreadInfo(thread_id=tid)
            adapter = UiEventItemAdapter(thread_id=tid, turn_id="")
            self._adapters[tid] = adapter
            self._active_thread_id = tid
        notification = await adapter.handle(event)
        if notification is None:
            return
        await self._broadcast(notification.model_dump_json())

    async def broadcast_snapshot(self) -> None:
        if not self._clients:
            return
        await self._broadcast(self._encode_snapshot())

    # ── v2 JSON-RPC dispatch ──────────────────────────────────────────────

    async def dispatch_request(
        self, request: JsonRpcRequest,
    ) -> JsonRpcResult | JsonRpcError:
        return await self.methods.dispatch(request)

    # ── multi-thread management ───────────────────────────────────────────

    async def register_thread(self, thread_id: str, *, title: str = "") -> None:
        self._threads[thread_id] = ThreadInfo(thread_id=thread_id, title=title)
        self._adapters[thread_id] = UiEventItemAdapter(
            thread_id=thread_id, turn_id="",
        )

    async def unregister_thread(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)
        self._adapters.pop(thread_id, None)
        if self._active_thread_id == thread_id:
            self._active_thread_id = ""

    def list_threads(self) -> list[ThreadInfo]:
        return list(self._threads.values())

    async def switch_thread(self, thread_id: str) -> None:
        if thread_id not in self._threads:
            raise MethodParamsError(
                f"thread not found: {thread_id}",
                code=-32000,
            )
        info = self._threads[thread_id]
        if info.status == "running":
            raise MethodParamsError(
                f"thread is running: {thread_id}",
                code=ERR_TURN_IN_PROGRESS,
            )
        self._active_thread_id = thread_id
        await self.broadcast_snapshot()

    # ── snapshot encoding ─────────────────────────────────────────────────

    def _encode_snapshot(self) -> str:
        seq = self._next_seq()
        snapshot = self._build_workspace_snapshot()
        envelope = JsonRpcNotification(
            method="workspace.snapshot",
            params=snapshot.model_dump(),
        )
        return envelope.model_dump_json()

    def _build_workspace_snapshot(self) -> WorkspaceSnapshot:
        tree = self._tree_provider()
        transcript = tree_to_snapshot(tree, session_id=self._session_id)
        active_snapshot = ThreadSnapshot(
            thread_id=self._active_thread_id,
            revision=self._seq,
            nodes=transcript.nodes,
        )
        return WorkspaceSnapshot(
            threads=list(self._threads.values()),
            active_thread_id=self._active_thread_id,
            active_snapshot=active_snapshot,
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _broadcast(self, text: str) -> None:
        results = await asyncio.gather(
            *(client.send_text(text) for client in tuple(self._clients)),
            return_exceptions=True,
        )
        for client, result in zip(tuple(self._clients), results, strict=False):
            if isinstance(result, Exception):
                self._clients.discard(client)

    # ── default method registration ──────────────────────────────────────

    def _register_default_methods(self) -> None:
        m = self.methods

        # Terminal
        m.register("terminal.start", self._method_terminal_create)
        m.register("terminal.input", self._method_terminal_input)
        m.register("terminal.resize", self._method_terminal_resize)
        m.register("terminal.stop", self._method_terminal_close)

        # Diff review
        m.register("diff.review", self._method_diff_review_start)
        m.register("diff.decide", self._method_diff_review_decide)
        m.register("diff.apply", self._method_diff_review_apply)

        # Session CRUD
        m.register("session.create", self._method_session_create)
        m.register("session.fork", self._method_session_fork)
        m.register("session.delete", self._method_session_delete)
        m.register("session.rename", self._method_session_rename)
        m.register("session.switch", self._method_session_switch)
        m.register("session.list", self._method_session_list)

        # Command forwarding (submit / cancel)
        m.register("session.submit", self._method_session_submit)
        m.register("session.cancel", self._method_session_cancel)

    # ── terminal methods ──────────────────────────────────────────────────

    async def _method_terminal_create(self, params: dict) -> dict:
        command = params.get("command", [])
        if not command:
            raise MethodParamsError("command is required")
        cols = params.get("cols", 80)
        rows = params.get("rows", 25)
        cwd = params.get("cwd")
        session = await self.terminal_manager.create(
            command, cols=cols, rows=rows, cwd=cwd,
        )
        self._start_terminal_output_reader(session)
        return {"terminal_id": session.terminal_id, "pid": session.pid}

    async def _method_terminal_input(self, params: dict) -> dict:
        terminal_id = params.get("terminal_id", "")
        data = params.get("data", "")
        session = self.terminal_manager.get(terminal_id)
        if session is None:
            raise MethodParamsError(f"terminal not found: {terminal_id}")
        await session.write(data)
        return {"written": True}

    async def _method_terminal_resize(self, params: dict) -> dict:
        terminal_id = params.get("terminal_id", "")
        cols = params.get("cols", 80)
        rows = params.get("rows", 25)
        session = self.terminal_manager.get(terminal_id)
        if session is None:
            raise MethodParamsError(f"terminal not found: {terminal_id}")
        await session.resize(cols=cols, rows=rows)
        return {"cols": cols, "rows": rows}

    async def _method_terminal_close(self, params: dict) -> dict:
        terminal_id = params.get("terminal_id", "")
        await self.terminal_manager.close(terminal_id)
        return {"closed": True}

    def _start_terminal_output_reader(self, session: TerminalSession) -> None:
        from voidx.ui.gateway.terminal import TerminalOutput

        async def _reader() -> None:
            async for data in session.read():
                output = TerminalOutput(terminal_id=session.terminal_id, data=data)
                notification = JsonRpcNotification(
                    method="terminal.output",
                    params=output.to_notification_params(),
                )
                await self._broadcast(notification.model_dump_json())

        asyncio.create_task(_reader())

    # ── diff review methods ───────────────────────────────────────────────

    def _method_diff_review_start(self, params: dict) -> dict:
        diff_text = params.get("diff", "")
        if not diff_text:
            raise MethodParamsError("diff is required")
        review_id = uuid.uuid4().hex[:12]
        review = DiffReviewSession.from_diff(diff_text)
        self._diff_reviews[review_id] = review
        return {"review_id": review_id, "snapshot": review.to_snapshot()}

    def _method_diff_review_decide(self, params: dict) -> dict:
        review_id = params.get("review_id", "")
        review = self._diff_reviews.get(review_id)
        if review is None:
            raise MethodParamsError(f"review not found: {review_id}")
        file_path = params.get("file_path", "")
        hunk_index = params.get("hunk_index", -1)
        decision = params.get("decision", "")
        review.decide(file_path, hunk_index, decision)
        return {"summary": review.summary()}

    def _method_diff_review_apply(self, params: dict) -> dict:
        review_id = params.get("review_id", "")
        review = self._diff_reviews.get(review_id)
        if review is None:
            raise MethodParamsError(f"review not found: {review_id}")
        changed = review.apply()
        return {"files_changed": changed}

    # ── session CRUD methods ──────────────────────────────────────────────

    async def _method_session_create(self, params: dict) -> dict:
        from voidx.memory.session import create_session
        title = params.get("title", "New session")
        info = await create_session(title=title)
        await self.register_thread(info.id, title=info.title)
        return {
            "thread_id": info.id,
            "title": info.title,
            "status": "idle",
        }

    async def _method_session_fork(self, params: dict) -> dict:
        from voidx.memory.session import fork_session
        thread_id = params.get("thread_id", "")
        title = params.get("title")
        info = await fork_session(thread_id, title=title)
        if info is None:
            raise MethodParamsError(f"thread not found: {thread_id}")
        await self.register_thread(info.id, title=info.title)
        return {
            "thread_id": info.id,
            "title": info.title,
            "status": "idle",
        }

    async def _method_session_delete(self, params: dict) -> dict:
        from voidx.memory.session import delete_session
        thread_id = params.get("thread_id", "")
        await delete_session(thread_id)
        await self.unregister_thread(thread_id)
        return {"ok": True}

    async def _method_session_rename(self, params: dict) -> dict:
        from voidx.memory.session import update_title
        thread_id = params.get("thread_id", "")
        title = params.get("title", "")
        await update_title(thread_id, title)
        info = self._threads.get(thread_id)
        if info is not None:
            self._threads[thread_id] = info.model_copy(update={"title": title})
        return {"ok": True}

    async def _method_session_switch(self, params: dict) -> dict:
        thread_id = params.get("thread_id", "")
        await self.switch_thread(thread_id)
        return {"active_thread_id": self._active_thread_id}

    def _method_session_list(self, params: dict) -> dict:
        return {
            "threads": [t.model_dump() for t in self._threads.values()],
        }

    async def _method_session_submit(self, params: dict) -> dict:
        from voidx.ui.protocol import UiSubmitCommand
        text = params.get("text", "")
        if not text:
            raise MethodParamsError("text is required")
        await self.handle_command(UiSubmitCommand(text=text))
        return {"ok": True}

    async def _method_session_cancel(self, params: dict) -> dict:
        from voidx.ui.protocol import UiCancelCommand
        await self.handle_command(UiCancelCommand())
        return {"ok": True}


class GatewayEventConsumer:
    """UiEventBus consumer that mirrors events to a GatewaySession."""

    def __init__(self, session: GatewaySession) -> None:
        self._session = session

    async def handle(self, event: UiEvent) -> None:
        await self._session.broadcast_event(event)
        if event.kind == "refresh.requested":
            await self._session.broadcast_snapshot()
