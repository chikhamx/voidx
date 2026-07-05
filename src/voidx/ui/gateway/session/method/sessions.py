"""Session CRUD and command JSON-RPC method handlers for GatewaySession."""

from __future__ import annotations

from voidx.ui.protocol.v2.methods import MethodParamsError


class SessionMethods:
    """Session/command-related JSON-RPC handlers, mixed into GatewaySession."""

    async def _method_session_create(self, params: dict) -> dict:
        from voidx.memory.session import create_session
        title = params.get("title", "New session")
        directory = params.get("directory", "")
        info = await create_session(
            workspace=self._workspace or ".",
            title=title,
            directory=directory,
        )
        await self.register_thread(info.id, title=info.title, directory=info.directory)
        return {
            "thread_id": info.id,
            "title": info.title,
            "directory": info.directory,
            "status": "idle",
        }

    async def _method_session_fork(self, params: dict) -> dict:
        from voidx.memory.session import fork_session
        thread_id = params.get("thread_id", "")
        title = params.get("title")
        info = await fork_session(thread_id, title=title)
        if info is None:
            raise MethodParamsError(f"thread not found: {thread_id}")
        await self.register_thread(info.id, title=info.title, directory=info.directory)
        return {
            "thread_id": info.id,
            "title": info.title,
            "directory": info.directory,
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

    async def _method_session_list(self, params: dict) -> dict:
        await self.sync_persisted_threads()
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
