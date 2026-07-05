"""Terminal JSON-RPC method handlers for GatewaySession."""

from __future__ import annotations

import asyncio

from voidx.ui.gateway.terminal import TerminalSession
from voidx.ui.protocol.v2.envelope import JsonRpcNotification
from voidx.ui.protocol.v2.methods import MethodParamsError


class TerminalMethods:
    """Terminal-related JSON-RPC handlers, mixed into GatewaySession."""

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
