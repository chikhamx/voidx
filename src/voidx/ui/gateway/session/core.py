"""v2 JSON-RPC gateway session core: connection, broadcast, threads, snapshot."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol

from voidx.ui.gateway.adapter import UiEventItemAdapter
from voidx.ui.gateway.diff_review import DiffReviewSession
from voidx.ui.gateway.terminal import TerminalManager
from voidx.ui.output.events.schema import UiEvent
from voidx.ui.output.tree import OutputTree
from voidx.ui.protocol import (
    TranscriptSnapshot,
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
)
from voidx.ui.protocol.v2.methods import MethodDispatch, MethodParamsError
from voidx.ui.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.ui.protocol.v2.threads import ThreadInfo

from voidx.ui.gateway.session.method.diff import DiffMethods
from voidx.ui.gateway.session.method.integrations import IntegrationMethods
from voidx.ui.gateway.session.method.sessions import SessionMethods
from voidx.ui.gateway.session.method.settings import SettingsMethods
from voidx.ui.gateway.session.method.terminal import TerminalMethods


RuntimeStateProvider = Callable[[], dict[str, object]]


class ProtocolClient(Protocol):
    async def send_text(self, text: str) -> None:
        """Send an encoded protocol envelope to the connected client."""


class GatewaySession(
    TerminalMethods,
    DiffMethods,
    SessionMethods,
    SettingsMethods,
    IntegrationMethods,
):
    """v2 JSON-RPC gateway session with multi-thread support."""

    def __init__(
        self,
        tree_providers: Callable[[], OutputTree],
        *,
        thread_id: str = "",
        session_id: str = "",
        command_handler: Callable[[UiCommand], Awaitable[None] | None] | None = None,
        workspace: str = "",
        runtime_state_provider: RuntimeStateProvider | None = None,
    ) -> None:
        self._tree_provider = tree_providers
        self._session_id = session_id or thread_id
        self._command_handler = command_handler
        self._workspace = workspace
        self._runtime_state_provider = runtime_state_provider
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
            await client.send_text(await self._encode_snapshot())
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
        await self._broadcast(await self._encode_snapshot())

    # ── v2 JSON-RPC dispatch ──────────────────────────────────────────────

    async def dispatch_request(
        self, request: JsonRpcRequest,
    ) -> JsonRpcResult | JsonRpcError:
        return await self.methods.dispatch(request)

    # ── multi-thread management ───────────────────────────────────────────

    async def register_thread(self, thread_id: str, *, title: str = "", directory: str = "") -> None:
        self._threads[thread_id] = ThreadInfo(thread_id=thread_id, title=title, directory=directory)
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

    async def sync_persisted_threads(self) -> None:
        from pathlib import Path

        from voidx.memory.session import SessionInfo, list_sessions

        def from_session(info: SessionInfo) -> ThreadInfo:
            return ThreadInfo(
                thread_id=info.id,
                title=info.title,
                workspace=info.workspace,
                directory=info.directory,
                model_provider=info.model_provider,
                model_name=info.model_name,
                status="idle",
                created_at=info.created_at,
                updated_at=info.updated_at,
                message_count=info.message_count,
            )

        for info in await list_sessions(limit=200):
            if info.id in self._threads:
                existing = self._threads[info.id]
                self._threads[info.id] = from_session(info).model_copy(
                    update={"status": existing.status},
                )
                continue
            self._threads[info.id] = from_session(info)
            self._adapters[info.id] = UiEventItemAdapter(thread_id=info.id, turn_id="")

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

    async def _encode_snapshot(self) -> str:
        self._next_seq()
        snapshot = await self._build_workspace_snapshot()
        envelope = JsonRpcNotification(
            method="workspace.snapshot",
            params=snapshot.model_dump(),
        )
        return envelope.model_dump_json()

    async def _build_workspace_snapshot(self) -> WorkspaceSnapshot:
        await self.sync_persisted_threads()
        transcript = await self._active_thread_snapshot()
        active_snapshot = ThreadSnapshot(
            thread_id=self._active_thread_id,
            revision=self._seq,
            nodes=transcript.nodes,
        )
        runtime_state = self._runtime_state_provider() if self._runtime_state_provider else {}
        return WorkspaceSnapshot(
            threads=list(self._threads.values()),
            active_thread_id=self._active_thread_id,
            active_snapshot=active_snapshot,
            provider=str(runtime_state.get("provider") or ""),
            model=str(runtime_state.get("model") or ""),
            workspace=str(runtime_state.get("workspace") or self._workspace),
            profile_configured=(
                runtime_state["profile_configured"]
                if isinstance(runtime_state.get("profile_configured"), bool)
                else None
            ),
        )

    async def _active_thread_snapshot(self) -> TranscriptSnapshot:
        if self._active_thread_id and self._active_thread_id != self._session_id:
            from voidx.memory.service import load_transcript

            rows = await load_transcript(self._active_thread_id)
            if rows:
                from voidx.ui.transcript import transcript_rows_to_tree

                return tree_to_snapshot(
                    transcript_rows_to_tree(rows),
                    session_id=self._active_thread_id,
                )
            return TranscriptSnapshot(session_id=self._active_thread_id, nodes=[])
        return tree_to_snapshot(self._tree_provider(), session_id=self._session_id)

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
        m.register("diff.generate", self._method_diff_generate)

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
        m.register("commands.list", self._method_commands_list)
        m.register("commands.run", self._method_commands_run)
        m.register("settings.get", self._method_settings_get)
        m.register("settings.update", self._method_settings_update)
        m.register("integrations.get", self._method_integrations_get)
        m.register("mcp.list", self._method_mcp_list)
        m.register("mcp.test", self._method_mcp_test)
        m.register("mcp.tools", self._method_mcp_tools)
        m.register("mcp.restart", self._method_mcp_restart)
        m.register("mcp.setDisabled", self._method_mcp_set_disabled)
        m.register("mcp.delete", self._method_mcp_delete)
        m.register("skills.list", self._method_skills_list)
        m.register("skills.show", self._method_skills_show)
        m.register("skills.setEnabled", self._method_skills_set_enabled)
        m.register("skills.setAuto", self._method_skills_set_auto)
        m.register("lsp.status", self._method_lsp_status)
        m.register("lsp.doctor", self._method_lsp_doctor)
        m.register("lsp.restart", self._method_lsp_restart)
        m.register("tavily.set", self._method_tavily_set)
        m.register("tavily.delete", self._method_tavily_delete)
