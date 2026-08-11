"""Session CRUD and command JSON-RPC method handlers for GatewaySession."""

from __future__ import annotations

from voidx.presentation.protocol.v2.methods import MethodParamsError


class SessionMethods:
    """Session/command-related JSON-RPC handlers, mixed into GatewaySession."""

    async def _method_session_create(self, params: dict) -> dict:
        from voidx.presentation.gateway.session.temporary import (
            new_temporary_thread_id,
            validate_temporary_profile,
        )

        title = params.get("title", "New session")
        directory = str(params.get("directory", "") or "")
        workspace = directory or self._workspace or "."
        profile = str(params.get("profile", "") or "coding")
        try:
            profile = validate_temporary_profile(profile)
        except ValueError as exc:
            raise MethodParamsError(f"unknown profile: {profile}") from exc
        thread_id = new_temporary_thread_id()
        await self.register_thread(
            thread_id,
            title=title,
            directory=directory,
            workspace=workspace,
            runtime_profile=profile,
            temporary=True,
        )
        self._active_thread_id = thread_id
        await self.broadcast_snapshot()
        return {
            "thread_id": thread_id,
            "active_thread_id": thread_id,
            "title": title,
            "directory": directory,
            "workspace": workspace,
            "status": "idle",
            "runtime_profile": profile,
            "temporary": True,
        }

    async def _method_session_fork(self, params: dict) -> dict:
        repository = self._session_repository
        if repository is None:
            raise RuntimeError("session_repository is required")
        thread_id = params.get("thread_id", "")
        title = params.get("title")
        info = await repository.fork_session(thread_id, title=title)
        if info is None:
            raise MethodParamsError(f"thread not found: {thread_id}")
        await self.register_thread(
            info.id,
            title=info.title,
            directory=info.directory,
            workspace=info.workspace,
            runtime_profile=info.runtime_profile,
        )
        return {
            "thread_id": info.id,
            "title": info.title,
            "directory": info.directory,
            "workspace": info.workspace,
            "runtime_profile": info.runtime_profile,
            "status": "idle",
        }

    async def _method_session_delete(self, params: dict) -> dict:
        thread_id = params.get("thread_id", "")
        info = self._threads.get(thread_id)
        if info is None or not info.temporary:
            repository = self._session_repository
            if repository is None:
                raise RuntimeError("session_repository is required")
            await repository.delete_session(thread_id)
        await self.unregister_thread(thread_id)
        return {"ok": True}

    async def _method_session_rename(self, params: dict) -> dict:
        thread_id = params.get("thread_id", "")
        title = params.get("title", "")
        info = self._threads.get(thread_id)
        if info is None or not info.temporary:
            repository = self._session_repository
            if repository is None:
                raise RuntimeError("session_repository is required")
            await repository.update_title(thread_id, title)
        if info is not None:
            self._threads[thread_id] = info.model_copy(update={"title": title})
        return {"ok": True}

    async def _method_session_switch(self, params: dict) -> dict:
        thread_id = params.get("thread_id", "")
        await self.switch_thread(thread_id)
        info = self._threads.get(self._active_thread_id)
        return {
            "active_thread_id": self._active_thread_id,
            "runtime_profile": info.runtime_profile if info else "coding",
        }

    async def _method_session_list(self, params: dict) -> dict:
        await self.sync_persisted_threads()
        return {
            "threads": [t.model_dump() for t in self._threads.values()],
        }

    async def _method_session_submit(self, params: dict) -> dict:
        from voidx.presentation.protocol import UiSubmitCommand
        text = params.get("text", "")
        if not text:
            raise MethodParamsError("text is required")
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        if not thread_id:
            thread_id = await self.ensure_active_thread()
        ok = await self.handle_command(UiSubmitCommand(text=text, thread_id=thread_id))
        return {"ok": bool(ok)}

    async def _method_session_cancel(self, params: dict) -> dict:
        from voidx.presentation.protocol import UiCancelCommand
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        await self.handle_command(UiCancelCommand(thread_id=thread_id))
        return {"ok": True}

    async def _method_session_respond(self, params: dict) -> dict:
        from voidx.presentation.protocol import UiResponse

        request_id = str(params.get("request_id", ""))
        if not request_id:
            raise MethodParamsError("request_id is required")
        value = params.get("value")
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        await self.handle_response(
            UiResponse(
                request_id=request_id,
                value=None if value is None else str(value),
            ),
            thread_id=thread_id,
        )
        return {"ok": True}

    def _method_usage_get(self, params: dict) -> dict:
        provider = getattr(self, "_usage_stats_provider", None)
        if provider is None:
            return {"usage": {}}
        stats = provider()
        if stats is None:
            return {"usage": {}}
        cache_hit_rate = stats.cache_hit_rate
        return {
            "usage": {
                "context_tokens": stats.context_tokens,
                "context_limit": stats.context_limit,
                "total_tokens": stats.total_tokens,
                "cache_hit_rate": cache_hit_rate,
                "cache_hit_rate_estimated": (
                    cache_hit_rate is not None and stats.cache_hit_rate_is_estimated
                ),
            }
        }

    def _method_commands_list(self, params: dict) -> dict:
        from voidx.presentation.command_catalog import command_catalog_dicts

        return {"commands": command_catalog_dicts()}

    async def _method_commands_run(self, params: dict) -> dict:
        from voidx.presentation.command_catalog import find_command
        from voidx.presentation.protocol import UiSubmitCommand

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

        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        await self.handle_command(UiSubmitCommand(text=value, thread_id=thread_id))
        return {"ok": True, "status": "submitted"}
