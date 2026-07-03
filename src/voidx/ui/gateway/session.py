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
        workspace: str = "",
    ) -> None:
        self._tree_provider = tree_provider
        self._session_id = session_id or thread_id
        self._command_handler = command_handler
        self._workspace = workspace
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

    def _method_diff_generate(self, params: dict) -> dict:
        import subprocess

        cwd = self._workspace or None
        try:
            result = subprocess.run(
                ["git", "diff", "--unified=3"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=10,
            )
            return {"diff": result.stdout.strip()}
        except Exception:
            return {"diff": ""}

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



    def _method_commands_list(self, params: dict) -> dict:
        from voidx.ui.command_catalog import command_catalog_dicts

        return {"commands": command_catalog_dicts()}

    async def _method_commands_run(self, params: dict) -> dict:
        from voidx.ui.command_catalog import find_command
        from voidx.ui.protocol import UiSubmitCommand

        text = params.get("text", "")
        mode = params.get("mode", "submit")
        confirmed = bool(params.get("confirmed"))
        if not isinstance(text, str) or not text.strip():
            raise MethodParamsError("text is required")
        if mode not in {"validate", "submit"}:
            raise MethodParamsError("invalid mode")

        item = find_command(text)
        if item is None:
            raise MethodParamsError("invalid command")

        item_dict = item.to_dict()
        if mode == "validate":
            return {"ok": True, "status": "valid", "item": item_dict}

        value = text.strip()
        lowered = value.lower()
        command = item.command.lower()
        has_args = lowered != command and lowered.startswith(command + " ")

        if item.dangerous and not confirmed:
            raise MethodParamsError("confirmation required")
        if item.execution == "open-ui":
            return {"ok": True, "action": "open-ui", "uiTarget": item.uiTarget or ""}
        if (item.requiresArgs or item.execution == "fill") and not has_args:
            raise MethodParamsError("command requires arguments")

        await self.handle_command(UiSubmitCommand(text=value))
        return {"ok": True, "status": "submitted"}

    async def _method_settings_get(self, params: dict) -> dict:
        from voidx.config.settings import Settings

        settings = Settings(self._workspace or ".")
        return await self._desktop_settings_snapshot(settings)

    async def _method_settings_update(self, params: dict) -> dict:
        from voidx.config.enums import ApprovalPolicy, CodeIde, PermissionMode, SandboxMode
        from voidx.config.models import ParallelSubagentsConfig, Profile
        from voidx.config.settings import Settings

        patch = params.get("patch", {})
        if not isinstance(patch, dict):
            raise MethodParamsError("patch is required")

        settings = Settings(self._workspace or ".")

        permissions = patch.get("permissions")
        if permissions is not None:
            if not isinstance(permissions, dict):
                raise MethodParamsError("invalid permissions")
            if "permission_mode" in permissions:
                try:
                    settings.set_permission_mode(PermissionMode(permissions["permission_mode"]))
                except ValueError as exc:
                    raise MethodParamsError("invalid permission_mode") from exc
            if "sandbox_mode" in permissions:
                try:
                    settings.set_sandbox_mode(SandboxMode(permissions["sandbox_mode"]))
                except ValueError as exc:
                    raise MethodParamsError("invalid sandbox_mode") from exc
            if "approval_policy" in permissions:
                try:
                    settings.set_approval_policy(ApprovalPolicy(permissions["approval_policy"]))
                except ValueError as exc:
                    raise MethodParamsError("invalid approval_policy") from exc
            if "sandbox_workspace_write" in permissions:
                paths = permissions["sandbox_workspace_write"]
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                    raise MethodParamsError("invalid sandbox_workspace_write")
                settings.set_sandbox_workspace_write(paths)

        user_profile = patch.get("user_profile")
        if user_profile is not None:
            if not isinstance(user_profile, dict):
                raise MethodParamsError("invalid user_profile")
            if "language" in user_profile:
                settings.set_user_language(str(user_profile["language"] or ""))
            if "tone" in user_profile:
                settings.set_user_tone(str(user_profile["tone"] or ""))

        parallel = patch.get("parallel_subagents")
        if parallel is not None:
            if not isinstance(parallel, dict):
                raise MethodParamsError("invalid parallel_subagents")
            try:
                current = settings.get_parallel_subagents()
                settings.set_parallel_subagents(ParallelSubagentsConfig(
                    enabled=bool(parallel.get("enabled", current.enabled)),
                    max_concurrent=int(parallel.get("max_concurrent", current.max_concurrent)),
                ))
            except Exception as exc:
                raise MethodParamsError("invalid parallel_subagents") from exc

        update_check = patch.get("update_check")
        if update_check is not None:
            if not isinstance(update_check, dict):
                raise MethodParamsError("invalid update_check")
            if "enabled" in update_check:
                settings.set_update_check_enabled(bool(update_check["enabled"]))

        if "code_ide" in patch:
            try:
                settings.set_code_ide(CodeIde(patch["code_ide"]))
            except ValueError as exc:
                raise MethodParamsError("invalid code_ide") from exc

        # model reconfiguration
        model_patch = patch.get("model")
        if model_patch is not None:
            if not isinstance(model_patch, dict):
                raise MethodParamsError("invalid model")
            provider = model_patch.get("provider") or "anthropic"
            model_name = model_patch.get("model") or "claude-sonnet-4-6"
            profile_name = f"{provider}/{model_name}"
            try:
                await settings.save_profile(Profile(
                    name=profile_name,
                    base_url=model_patch.get("base_url"),
                    protocol=model_patch.get("protocol"),
                ))
            except Exception as exc:
                raise MethodParamsError(f"model save failed: {exc}") from exc

        # reasoning / context
        if "reasoning_effort" in model_patch if model_patch else {}:
            valid_effort = {"off", "low", "medium", "high", "xhigh"}
            effort = str(model_patch["reasoning_effort"] or "")
            if effort and effort not in valid_effort:
                raise MethodParamsError(f"invalid reasoning_effort: {effort}")
            settings._set_setting("reasoning_effort", effort or None)

        if "context_window" in (model_patch or {}):
            ctx = model_patch["context_window"]
            if ctx is not None and (not isinstance(ctx, int) or ctx < 1):
                raise MethodParamsError("invalid context_window")
            if ctx is None:
                settings._pop_setting("context_window")
            else:
                settings._set_setting("context_window", ctx)

        # provider secrets
        secrets_patch = patch.get("provider_secrets")
        if secrets_patch is not None:
            if not isinstance(secrets_patch, dict):
                raise MethodParamsError("invalid provider_secrets")
            provider = secrets_patch.get("provider", "")
            action = secrets_patch.get("action", "set")
            if not provider:
                raise MethodParamsError("provider is required")
            if action not in ("set", "delete"):
                raise MethodParamsError("invalid action")
            if action == "set":
                api_key = secrets_patch.get("api_key", "")
                if not isinstance(api_key, str) or not api_key.strip():
                    raise MethodParamsError("api_key is required")
                # build profile name from provider + first known model
                profile_name = secrets_patch.get("profile_name")
                if not profile_name:
                    # find existing profile for this provider
                    existing = await settings.list_profiles()
                    match = next((p for p in existing if p.provider == provider), None)
                    profile_name = match.name if match else f"{provider}/default"
                await settings.save_profile(Profile(name=profile_name, api_key=api_key.strip()))
            else:
                profile_name = secrets_patch.get("profile_name")
                if not profile_name:
                    existing = await settings.list_profiles()
                    match = next((p for p in existing if p.provider == provider), None)
                    if match is None:
                        raise MethodParamsError("no profile found for provider")
                    profile_name = match.name
                await settings.delete_profile(profile_name)

        return {"ok": True, "settings": await self._desktop_settings_snapshot(settings)}

    async def _desktop_settings_snapshot(self, settings) -> dict:
        profile = await settings.resolve_profile()
        profiles = await settings.list_profiles()
        model = {
            "provider": profile.provider if profile else "anthropic",
            "model": profile.model if profile else "claude-sonnet-4-6",
            "base_url": profile.base_url if profile else None,
            "protocol": profile.protocol if profile else None,
            "reasoning_effort": settings._effective_data().get("reasoning_effort") or "xhigh",
            "context_window": settings._effective_data().get("context_window"),
        }
        parallel = settings.get_parallel_subagents()
        return {
            "model": model,
            "profiles": [
                {
                    "name": profile_item.name,
                    "provider": profile_item.provider,
                    "model": profile_item.model,
                    "base_url": profile_item.base_url,
                    "protocol": profile_item.protocol,
                    "configured": bool(profile_item.api_key),
                }
                for profile_item in profiles
            ],
            "permissions": {
                "permission_mode": settings.get_permission_mode().value,
                "sandbox_mode": settings.get_sandbox_mode().value,
                "approval_policy": settings.get_approval_policy().value,
                "approval_reviewer": settings.get_approval_reviewer().value,
                "sandbox_workspace_write": settings.get_sandbox_workspace_write(),
            },
            "user_profile": settings.get_user_profile().model_dump(),
            "code_ide": settings.get_code_ide().value,
            "update_check": {
                "enabled": settings.get_update_check_enabled(),
                "last_checked_at": settings.get_update_check_last_checked_at(),
                "latest_version": settings.get_update_check_latest_version(),
            },
            "parallel_subagents": parallel.model_dump(),
            "paths": {
                "workspace_settings": str(settings.path),
                "global_settings": str(settings._global_path),
                "skills_state": str(settings.skills_path),
            },
        }

    async def _method_integrations_get(self, params: dict) -> dict:
        settings = self._gateway_settings()
        return {
            "mcp_servers": [self._mcp_server_summary(server) for server in settings.list_mcp_servers()],
            "web_routes": {
                "search": settings.get_web_tool_route("search").model_dump(),
                "fetch": settings.get_web_tool_route("fetch").model_dump(),
            },
            "tavily": self._tavily_summary(settings),
            "skills": self._skill_summaries(settings),
            "lsp": await self._lsp_status_list(),
            "warnings": [],
        }

    def _method_mcp_list(self, params: dict) -> dict:
        settings = self._gateway_settings()
        return {"servers": [self._mcp_server_summary(server) for server in settings.list_mcp_servers()]}

    async def _method_mcp_test(self, params: dict) -> dict:
        settings = self._gateway_settings()
        server = self._require_mcp_server(settings, params.get("name", ""))
        if server.disabled:
            raise MethodParamsError("disabled server")
        return {
            "ok": True,
            "server": self._mcp_server_summary(server),
            "message": "Configuration found. Live connection testing is available after the MCP manager starts.",
        }

    def _method_mcp_tools(self, params: dict) -> dict:
        settings = self._gateway_settings()
        server = self._require_mcp_server(settings, params.get("name", ""))
        return {"tools": self._mcp_tool_summaries(server)}

    async def _method_mcp_restart(self, params: dict) -> dict:
        settings = self._gateway_settings()
        server = self._require_mcp_server(settings, params.get("name", ""))
        return {"ok": True, "server": self._mcp_server_summary(server)}

    def _method_mcp_set_disabled(self, params: dict) -> dict:
        settings = self._gateway_settings()
        name = params.get("name", "")
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        try:
            settings.set_mcp_server_disabled(name, bool(params.get("disabled")))
        except KeyError as exc:
            raise MethodParamsError("server not found") from exc
        return {"ok": True, "server": self._mcp_server_summary(self._require_mcp_server(settings, name))}

    def _method_mcp_delete(self, params: dict) -> dict:
        settings = self._gateway_settings()
        name = params.get("name", "")
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        if not params.get("confirmed"):
            raise MethodParamsError("confirmation required")
        if settings.get_mcp_server(name) is None:
            raise MethodParamsError("server not found")
        settings.delete_mcp_server(name)
        return {"ok": True}

    def _method_skills_list(self, params: dict) -> dict:
        return {"skills": self._skill_summaries(self._gateway_settings())}

    def _method_skills_show(self, params: dict) -> dict:
        service = self._skill_service(self._gateway_settings())
        name = params.get("name", "")
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        skill = service.get(name)
        if skill is None:
            raise MethodParamsError("skill not found")
        return {"skill": self._skill_detail(service, skill)}

    def _method_skills_set_enabled(self, params: dict) -> dict:
        settings = self._gateway_settings()
        service = self._skill_service(settings)
        name = params.get("name", "")
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        if service.get(name) is None:
            raise MethodParamsError("skill not found")
        settings.set_skill_enabled(name, bool(params.get("enabled")))
        return {"ok": True, "skills": self._skill_summaries(settings)}

    def _method_skills_set_auto(self, params: dict) -> dict:
        settings = self._gateway_settings()
        service = self._skill_service(settings)
        name = params.get("name", "")
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        if service.get(name) is None:
            raise MethodParamsError("skill not found")
        settings.set_skill_auto(name, bool(params.get("auto")))
        return {"ok": True, "skills": self._skill_summaries(settings)}

    async def _method_lsp_status(self, params: dict) -> dict:
        return {"servers": await self._lsp_status_list()}

    async def _method_lsp_doctor(self, params: dict) -> dict:
        manager = await self._new_lsp_manager()
        checks = [check.model_dump() for check in manager.doctor()]
        return {"ok": all((not check.get("enabled")) or check.get("available") for check in checks), "checks": checks}

    async def _method_lsp_restart(self, params: dict) -> dict:
        manager = await self._new_lsp_manager()
        server = params.get("server")
        if server is not None and not isinstance(server, str):
            raise MethodParamsError("invalid server")
        await manager.restart(server or None)
        return {"ok": True, "servers": [status.model_dump() for status in manager.statuses()]}

    def _gateway_settings(self):
        from voidx.config.settings import Settings
        return Settings(self._workspace or ".")

    def _mcp_server_summary(self, server) -> dict:
        return {
            "name": server.name,
            "transport": server.effective_transport,
            "disabled": server.disabled,
            "tool_count": server.tool_count,
            "command": server.command,
            "url": server.url,
            "tools": [tool["name"] for tool in self._mcp_tool_summaries(server)],
        }

    def _mcp_tool_summaries(self, server) -> list[dict]:
        tools = server.tools or []
        names = list(tools.keys()) if isinstance(tools, dict) else list(tools)
        return [{"name": name, "description": ""} for name in names]

    def _require_mcp_server(self, settings, name: str):
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        server = settings.get_mcp_server(name)
        if server is None:
            raise MethodParamsError("server not found")
        return server

    def _tavily_summary(self, settings) -> dict:
        import os
        key = settings.get_tavily_api_key()
        env_key = os.environ.get("TAVILY_API_KEY")
        data = settings._effective_data()
        source = "env" if env_key else ("settings" if data.get("tavily_api_key") else "none")
        summary = {"configured": bool(key), "source": source}
        if key:
            summary["masked_value"] = "****" if len(key) <= 8 else f"{key[:3]}...{key[-4:]}"
        return summary

    def _method_tavily_set(self, params: dict) -> dict:
        api_key = params.get("api_key", "")
        if not isinstance(api_key, str) or not api_key.strip():
            raise MethodParamsError("api_key is required")
        scope = params.get("scope", "global")
        if scope not in {"global", "workspace"}:
            raise MethodParamsError("invalid scope")
        settings = GatewaySession._settings_for_scope(scope, self._workspace or ".")
        settings.set_tavily_api_key(api_key.strip())
        return {"ok": True, "tavily": self._tavily_summary(settings)}

    def _method_tavily_delete(self, params: dict) -> dict:
        scope = params.get("scope", "global")
        if scope not in {"global", "workspace"}:
            raise MethodParamsError("invalid scope")
        settings = GatewaySession._settings_for_scope(scope, self._workspace or ".")
        settings.delete_tavily_api_key()
        return {"ok": True, "tavily": self._tavily_summary(settings)}

    @staticmethod
    def _settings_for_scope(scope: str, workspace: str):
        from voidx.config.settings import Settings
        if scope == "workspace" and workspace != ".":
            return Settings(workspace)
        return Settings(workspace)

    def _skill_service(self, settings):
        from voidx.skills.registry import SkillRegistry
        from voidx.skills.service import SkillService
        return SkillService(SkillRegistry(self._workspace or "."), selection=settings.get_skill_selection())

    def _skill_summaries(self, settings) -> list[dict]:
        service = self._skill_service(settings)
        return [{"name": skill.name, "scope": skill.meta.scope, "enabled": service.is_enabled(skill), "auto": service.is_auto(skill), "description": skill.meta.description, "path": str(skill.path)} for skill in service.list_skills()]

    def _skill_detail(self, service, skill) -> dict:
        return {"name": skill.name, "scope": skill.meta.scope, "enabled": service.is_enabled(skill), "auto": service.is_auto(skill), "description": skill.meta.description, "triggers": list(skill.meta.triggers), "path": str(skill.path), "body": skill.body}

    async def _new_lsp_manager(self):
        from voidx.lsp.manager import LspManager
        manager = LspManager(self._workspace or ".")
        try:
            await manager.initialize()
        except Exception:
            pass
        return manager

    async def _lsp_status_list(self) -> list[dict]:
        manager = await self._new_lsp_manager()
        return [status.model_dump() for status in manager.statuses()]


class GatewayEventConsumer:
    """UiEventBus consumer that mirrors events to a GatewaySession."""

    def __init__(self, session: GatewaySession) -> None:
        self._session = session

    async def handle(self, event: UiEvent) -> None:
        await self._session.broadcast_event(event)
        if event.kind == "refresh.requested":
            await self._session.broadcast_snapshot()
