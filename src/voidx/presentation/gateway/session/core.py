"""v2 JSON-RPC gateway session core: connection, broadcast, threads, snapshot."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from voidx.agent.ports.persistence import SessionRepository
from voidx.presentation.gateway.adapter import UiEventItemAdapter
from voidx.presentation.gateway.diff_review import DiffReviewSession
from voidx.presentation.gateway.run_manager import ThreadRunManager
from voidx.presentation.gateway.terminal import TerminalManager
from voidx.presentation.gateway.workspace_lock import GatewayWorkspaceWriteLock
from voidx.presentation.output.events.schema import UiEvent
from voidx.presentation.output.tree import OutputTree
from voidx.presentation.protocol import (
    TranscriptSnapshot,
    UiCommand,
    UiRequest,
    UiResponse,
    tree_to_snapshot,
)
from voidx.presentation.protocol.v2.envelope import (
    ERR_TURN_IN_PROGRESS,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
)
from voidx.presentation.protocol.v2.methods import MethodDispatch, MethodParamsError
from voidx.presentation.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.presentation.protocol.v2.threads import ThreadInfo

from voidx.presentation.gateway.session.method.diff import DiffMethods
from voidx.presentation.gateway.session.method.integrations import IntegrationMethods
from voidx.presentation.gateway.session.method.references import ReferenceMethods
from voidx.presentation.gateway.session.method.sessions import SessionMethods
from voidx.presentation.gateway.session.method.settings import SettingsMethods
from voidx.presentation.gateway.session.method.terminal import TerminalMethods


RuntimeStateProvider = Callable[[], dict[str, object]]
SettingsUpdateHandler = Callable[[object], Awaitable[None] | None]
SkillsApiFactory = Callable[[str], Awaitable[Any]]


class ProtocolClient(Protocol):
    async def send_text(self, text: str, *, priority: bool = False) -> None:
        """Send an encoded protocol envelope to the connected client."""


class GatewaySession(
    TerminalMethods,
    DiffMethods,
    SessionMethods,
    SettingsMethods,
    IntegrationMethods,
    ReferenceMethods,
):
    """v2 JSON-RPC gateway session with multi-thread support."""

    def __init__(
        self,
        tree_providers: Callable[[], OutputTree],
        *,
        thread_id: str = "",
        session_id: str = "",
        runtime_profile: str = "coding",
        command_handler: Callable[[UiCommand], Awaitable[None] | None] | None = None,
        workspace: str = "",
        runtime_state_provider: RuntimeStateProvider | None = None,
        settings_update_handler: SettingsUpdateHandler | None = None,
        mcp_catalog_provider: Callable[[], list] | None = None,
        usage_stats_provider: Callable[[], object] | None = None,
        settings_factory: Callable[[str], Awaitable[object]] | None = None,
        skills_api_factory: SkillsApiFactory | None = None,
        skills_api_provider: Callable[[str], object] | None = None,
        session_repository: SessionRepository | None = None,
    ) -> None:
        self._tree_provider = tree_providers
        self._session_id = session_id or thread_id
        self._command_handler = command_handler
        self._run_manager = ThreadRunManager(
            command_handler=self._dispatch_command,
            max_concurrent_sessions=2,
        )
        self._workspace_write_lock = GatewayWorkspaceWriteLock(self._run_manager)
        self._workspace = workspace
        self._runtime_state_provider = runtime_state_provider
        self._settings_update_handler = settings_update_handler
        self._mcp_catalog_provider = mcp_catalog_provider
        self._usage_stats_provider = usage_stats_provider
        self._clients: set[ProtocolClient] = set()
        self._settings_factory = settings_factory
        self._skills_api_factory = skills_api_factory
        self._skills_api_provider = skills_api_provider
        self._session_repository = session_repository
        self._owner_id = uuid.uuid4().hex
        self._seq = 0
        self._thread_id_provider: Callable[[], str] | None = None
        self._persisted_sync_task: asyncio.Task[None] | None = None

        # Multi-thread state
        self._threads: dict[str, ThreadInfo] = {}
        self._adapters: dict[str, UiEventItemAdapter] = {}
        self._active_thread_id = thread_id or ""

        if thread_id:
            self._threads[thread_id] = ThreadInfo(
                thread_id=thread_id,
                workspace=workspace or ".",
                runtime_profile=runtime_profile,
            )
            self._adapters[thread_id] = UiEventItemAdapter(
                thread_id=thread_id, turn_id="",
            )

        # v2 method dispatch
        self.methods = MethodDispatch()
        self.terminal_manager = TerminalManager()
        self._diff_reviews: dict[str, DiffReviewSession] = {}
        self._register_default_methods()

    # ── properties ────────────────────────────────────────────────────────

    async def initialize_provisional_lifecycle(self) -> list[str]:
        if self._session_repository is None:
            return []
        return await self._session_repository.initialize_provisional_owner(self._owner_id)

    async def close_provisional_lifecycle(self) -> int:
        if self._session_repository is None:
            return 0
        return await self._session_repository.close_provisional_owner(self._owner_id)

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def workspace_write_lock(self):
        return self._workspace_write_lock

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
            await client.send_text(await self._encode_snapshot(sync_persisted=False))
            self._start_persisted_thread_sync()
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

    async def handle_command(self, command: UiCommand) -> bool:
        thread_id = getattr(command, "thread_id", "") or self._active_thread_id or ""
        if command.kind == "submit":
            async with self._run_manager.submission_lock(thread_id):
                return await self._handle_submit(command, thread_id)
        if command.kind == "cancel":
            await self._run_manager.cancel(thread_id)
            self._sync_thread_status(thread_id)
            return True
        return await self._dispatch_command(command)


    async def _handle_submit(self, command: UiCommand, thread_id: str) -> bool:
        from voidx.presentation.gateway.session.temporary import is_work_submission

        info = self._threads.get(thread_id)
        if info is not None and info.temporary and not is_work_submission(command.text):
            return await self._dispatch_command(command)
        if self._run_manager.actor(thread_id).is_active:
            if command.text.lstrip().startswith("/"):
                return await self._dispatch_command(command)
            return await self._dispatch_command(
                {"kind": "guide", "text": command.text, "thread_id": thread_id}
            )
        staged = False
        try:
            staged = await self._stage_temporary_thread(thread_id)
            if staged:
                await self.broadcast_snapshot(sync_persisted=False)
            info = self._threads.get(thread_id)
            queued_command = command
            if info is not None:
                queued_command = command.model_copy(update={
                    "thread_id": thread_id,
                    "session_id": thread_id,
                    "runtime_profile": info.runtime_profile,
                    "workspace": info.workspace,
                })
            await self._run_manager.submit(queued_command)
        except MethodParamsError as exc:
            if staged:
                await self._rollback_temporary_thread(thread_id)
            if exc.code != ERR_TURN_IN_PROGRESS:
                raise
            if command.text.lstrip().startswith("/"):
                return await self._dispatch_command(command)
            return await self._dispatch_command(
                {"kind": "guide", "text": command.text, "thread_id": thread_id}
            )
        except BaseException:
            if staged:
                await self._rollback_temporary_thread(thread_id)
            raise
        self._sync_thread_status(thread_id)
        await self.broadcast_snapshot(sync_persisted=False)
        return True
    async def _stage_temporary_thread(self, thread_id: str) -> bool:
        info = self._threads.get(thread_id)
        repository = self._session_repository
        if info is None or not info.temporary or repository is None:
            return False
        await repository.stage_provisional_session(
            owner_id=self._owner_id,
            session_id=thread_id,
            workspace=info.workspace,
            directory=info.directory,
            title=info.title or "New session",
            profile=info.runtime_profile,
        )
        return True

    async def _rollback_temporary_thread(self, thread_id: str) -> None:
        if self._session_repository is not None:
            await self._session_repository.rollback_provisional_session(thread_id)
        self._run_manager.complete_turn(thread_id)
        self._sync_thread_status(thread_id)
        await self.broadcast_snapshot(sync_persisted=False)

    async def _dispatch_command(self, command: UiCommand | dict[str, Any]) -> bool:
        if self._command_handler is None:
            return True
        result = self._command_handler(command)
        if inspect.isawaitable(result):
            result = await result
        return True if result is None else bool(result)

    def _sync_thread_status(self, thread_id: str) -> None:
        info = self._threads.get(thread_id)
        if info is not None:
            self._threads[thread_id] = info.model_copy(
                update={"status": self._run_manager.status(thread_id)},
            )

    async def request(self, request: UiRequest) -> UiResponse | None:
        if not self._clients:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[UiResponse] = loop.create_future()
        thread_id = getattr(request, "thread_id", "") or self._active_thread_id or ""
        self._run_manager.register_pending_request(thread_id, request.request_id, future)
        params = request.model_dump()
        params["thread_id"] = thread_id
        notification = JsonRpcNotification(
            method="ui.request",
            params=params,
        )
        try:
            await self._broadcast(notification.model_dump_json())
            if not self._clients:
                return None
            return await future
        finally:
            self._run_manager.remove_pending_request(thread_id, request.request_id)

    async def handle_response(self, response: UiResponse, *, thread_id: str = "") -> None:
        tid = thread_id or getattr(response, "thread_id", "") or self._active_thread_id or ""
        if tid and self._run_manager.resolve_pending_request(tid, response):
            return
        self._run_manager.resolve_unique_pending_request(response)

    # ── v2 event broadcasting ─────────────────────────────────────────────

    def _single_active_run_thread_id(self) -> str:
        active = self._run_manager.active_thread_ids()
        return active[0] if len(active) == 1 else ""

    async def broadcast_event(self, event: UiEvent, *, thread_id: str = "") -> None:
        tid = thread_id or getattr(event, "thread_id", "")
        active_run_thread_id = self._single_active_run_thread_id()
        if not tid:
            tid = active_run_thread_id
        if not tid and not self._run_manager.active_thread_ids():
            tid = self._active_thread_id
        if not tid and self._thread_id_provider is not None:
            tid = self._thread_id_provider() or ""
        if not tid:
            return
        await self._apply_turn_terminal_event(event, tid)
        if not self._clients:
            return
        adapter = self._adapters.get(tid)
        if adapter is None:
            if tid not in self._threads:
                self._threads[tid] = ThreadInfo(thread_id=tid)
            adapter = UiEventItemAdapter(thread_id=tid, turn_id="")
            self._adapters[tid] = adapter
            self._active_thread_id = tid
        notification = await adapter.handle(event)
        if notification is not None:
            await self._broadcast(notification.model_dump_json())
        if getattr(event, "kind", "") in {"turn.completed", "turn.failed", "turn.cancelled"}:
            await self.broadcast_snapshot(sync_persisted=False)

    async def _apply_turn_terminal_event(self, event: UiEvent, thread_id: str) -> None:
        kind = getattr(event, "kind", "")
        info = self._threads.get(thread_id)
        if kind == "turn.completed":
            if info is not None and info.temporary and self._session_repository is not None:
                await self._session_repository.promote_provisional_session(thread_id)
                self._threads[thread_id] = info.model_copy(update={"temporary": False})
            self._run_manager.complete_turn(thread_id)
            self._sync_thread_status(thread_id)
            return
        if kind in {"turn.failed", "turn.cancelled"}:
            if info is not None and info.temporary:
                await self._rollback_temporary_thread(thread_id)
                return
            if kind == "turn.cancelled":
                self._run_manager.complete_turn(thread_id)
            else:
                self._run_manager.fail_turn(thread_id, getattr(event, "message", ""))
            self._sync_thread_status(thread_id)

    async def broadcast_snapshot(self, *, sync_persisted: bool = True) -> None:
        if not self._clients:
            return
        await self._broadcast(
            await self._encode_snapshot(sync_persisted=sync_persisted)
        )

    # ── v2 JSON-RPC dispatch ──────────────────────────────────────────────

    async def dispatch_request(
        self, request: JsonRpcRequest,
    ) -> JsonRpcResult | JsonRpcError:
        return await self.methods.dispatch(request)

    # ── multi-thread management ───────────────────────────────────────────

    async def register_thread(
        self,
        thread_id: str,
        *,
        title: str = "",
        directory: str = "",
        workspace: str = "",
        runtime_profile: str = "coding",
        temporary: bool = False,
    ) -> None:
        self._threads[thread_id] = ThreadInfo(
            thread_id=thread_id,
            title=title,
            workspace=workspace or self._workspace or ".",
            directory=directory,
            runtime_profile=runtime_profile,
            temporary=temporary,
        )
        self._adapters[thread_id] = UiEventItemAdapter(
            thread_id=thread_id, turn_id="",
        )
        if not self._active_thread_id:
            self._active_thread_id = thread_id

    async def unregister_thread(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)
        self._adapters.pop(thread_id, None)
        if self._active_thread_id == thread_id:
            self._active_thread_id = ""

    def has_thread(self, thread_id: str) -> bool:
        return thread_id in self._threads

    def list_threads(self) -> list[ThreadInfo]:
        return list(self._threads.values())

    async def ensure_active_thread(self) -> str:
        if self._active_thread_id:
            return self._active_thread_id
        if self._session_repository is None:
            raise RuntimeError("session_repository is required")

        runtime_state = self._runtime_state_provider() if self._runtime_state_provider else {}
        runtime_profile = str(runtime_state.get("runtime_profile") or "coding")
        info = await self._session_repository.create_session(
            workspace=self._workspace or ".",
            provider=str(runtime_state.get("provider") or "anthropic"),
            model=str(runtime_state.get("model") or ""),
            profile=runtime_profile,
        )
        await self.register_thread(
            info.id,
            title=info.title,
            directory=info.directory,
            workspace=info.workspace,
            runtime_profile=runtime_profile,
        )
        return info.id

    async def sync_persisted_threads(self) -> bool:
        from pathlib import Path

        if self._session_repository is None:
            return False

        def from_session(info: object) -> ThreadInfo:
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
                runtime_profile=getattr(info, "runtime_profile", "coding") or "coding",
            )

        changed = False
        for info in await self._session_repository.list_sessions(limit=200):
            if info.id in self._threads:
                existing = self._threads[info.id]
                updated = from_session(info).model_copy(
                    update={"status": existing.status},
                )
                if updated != existing:
                    changed = True
                    self._threads[info.id] = updated
                continue
            self._threads[info.id] = from_session(info)
            self._adapters[info.id] = UiEventItemAdapter(thread_id=info.id, turn_id="")
            changed = True
        return changed

    def _start_persisted_thread_sync(self) -> None:
        if self._persisted_sync_task is not None and not self._persisted_sync_task.done():
            return
        self._persisted_sync_task = asyncio.create_task(
            self._sync_persisted_threads_and_broadcast(),
        )

    async def _sync_persisted_threads_and_broadcast(self) -> None:
        changed = await self.sync_persisted_threads()
        if changed and self._clients:
            await self._broadcast(await self._encode_snapshot(sync_persisted=False))

    async def switch_thread(self, thread_id: str) -> None:
        if thread_id not in self._threads:
            raise MethodParamsError(
                f"thread not found: {thread_id}",
                code=-32000,
            )
        self._active_thread_id = thread_id
        await self.broadcast_snapshot()

    # ── snapshot encoding ─────────────────────────────────────────────────

    async def _encode_snapshot(self, *, sync_persisted: bool = True) -> str:
        self._next_seq()
        snapshot = await self._build_workspace_snapshot(sync_persisted=sync_persisted)
        envelope = JsonRpcNotification(
            method="workspace.snapshot",
            params=snapshot.model_dump(),
        )
        return envelope.model_dump_json()

    async def _build_workspace_snapshot(self, *, sync_persisted: bool = True) -> WorkspaceSnapshot:
        if sync_persisted:
            await self.sync_persisted_threads()
        transcript = await self._active_thread_snapshot()
        active_snapshot = ThreadSnapshot(
            thread_id=self._active_thread_id,
            revision=self._seq,
            nodes=transcript.nodes,
        )
        runtime_state = self._runtime_state_provider() if self._runtime_state_provider else {}
        for thread_id in self._run_manager.active_thread_ids():
            if thread_id not in self._threads:
                self._threads[thread_id] = ThreadInfo(thread_id=thread_id)
            self._sync_thread_status(thread_id)
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
            permission_mode=str(runtime_state.get("permission_mode") or ""),
            ai_approval_count=int(runtime_state.get("ai_approval_count") or 0),
            runtime=self._run_manager.runtime_snapshot(),
            workspace_write_lock=self._run_manager.workspace_write_lock_snapshot(),
        )

    async def _active_thread_snapshot(self) -> TranscriptSnapshot:
        if self._active_thread_id and self._active_thread_id != self._session_id:
            from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript

            rows = await load_transcript(self._active_thread_id)
            if rows:
                from voidx.presentation.adapters.persistence.transcript_snapshot import transcript_rows_to_tree

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
        m.register("session.respond", self._method_session_respond)
        m.register("commands.list", self._method_commands_list)
        m.register("usage.get", self._method_usage_get)
        m.register("commands.run", self._method_commands_run)
        m.register("settings.get", self._method_settings_get)
        m.register("settings.update", self._method_settings_update)

        # Reference candidates (@ files / # skills)
        m.register("attachments.candidates", self._method_attachments_candidates)
        m.register("attachments.saveImage", self._method_attachments_save_image)
        m.register("skills.candidates", self._method_skills_candidates)
        m.register("mcp.candidates", self._method_mcp_candidates)
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
        m.register("bocha.set", self._method_bocha_set)
        m.register("bocha.delete", self._method_bocha_delete)
