"""Stdio transport for the MCP client."""

from __future__ import annotations

import asyncio
import json

from voidx.mcp.client.errors import McpConnectionError
from voidx.logging.tool_log import log_tool_event
from voidx.runtime.processes import create_owned_subprocess_exec



class StdioTransportMixin:
    async def _spawn(self) -> None:
        """Spawn the subprocess with configured command/args/env."""
        cmd = self._config.command
        if not cmd:
            raise McpConnectionError(f"MCP server '{self._server_name}' has no command configured")
        args = [cmd] + list(self._config.args)

        env = None
        if self._config.env:
            import os
            env = {**os.environ, **self._config.env}

        try:
            self._proc = await create_owned_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._config.cwd,
            )
        except FileNotFoundError as e:
            raise McpConnectionError(
                f"Command not found for MCP server '{self._server_name}': {cmd}"
            ) from e
        except PermissionError as e:
            raise McpConnectionError(
                f"Permission denied for MCP server '{self._server_name}': {cmd}"
            ) from e

        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            await self._cleanup()
            raise McpConnectionError(f"Failed to open pipes for MCP server '{self._server_name}'")

        self._writer = self._proc.stdin
        self._reader = self._proc.stdout

        # Read stderr in background (logging only)
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # Start background reader for incoming JSON-RPC responses
        self._read_task = asyncio.create_task(self._read_responses())


    async def _send_stdio(self, payload: dict[str, Any]) -> None:
        """Send a JSON-RPC message over stdio."""
        if self._writer is None:
            raise McpConnectionError("stdio writer not available")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()


    async def _read_responses(self) -> None:
        """Read JSON-RPC responses from stdio stdout (background task)."""
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    log_tool_event("mcp_invalid_json", tool_name=self._server_name, message=f"Invalid JSON from MCP server '{self._server_name}': {text[:200]}")
                    continue
                self._dispatch_response(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_tool_event("mcp_stdio_exited", tool_name=self._server_name, message=f"stdio reader for '{self._server_name}' exited: {e}")
        finally:
            if self._healthy:
                self._healthy = False
                self._error_message = "stdio connection lost"
                for req in self._pending.values():
                    if not req.future.done():
                        req.future.set_exception(McpConnectionError("Connection lost"))
                self._pending.clear()


    async def _read_stderr(self) -> None:
        """Read stderr and forward to debug output."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log_tool_event("mcp_stderr", tool_name=self._server_name, message=text)
        except Exception as exc:
            log_tool_event("mcp_stderr_reader_failed", tool_name=self._server_name, message=str(exc))
