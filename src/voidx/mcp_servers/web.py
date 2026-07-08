"""Built-in line-delimited JSON-RPC MCP server for web tools."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from voidx.mcp.schema import MCP_PROTOCOL_VERSION
from voidx.tools.base import BaseTool, ToolContext
from voidx.tools.web import WebFetchTool, WebSearchTool


def _tools() -> dict[str, BaseTool]:
    return {
        "web_search": WebSearchTool(settings=None),
        "web_fetch": WebFetchTool(settings=None),
    }


async def _handle(message: dict[str, Any], tools: dict[str, BaseTool]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        params = message.get("params", {})
        protocol_version = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
        return _result(msg_id, {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "voidx-web", "version": "1.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _result(msg_id, {
            "tools": [
                {
                    "name": name,
                    "description": tool.description,
                    "inputSchema": tool.parameters_schema(),
                }
                for name, tool in tools.items()
            ]
        })

    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        tool = tools.get(name)
        if tool is None:
            return _error(msg_id, -32602, f"Unknown tool: {name}")
        result = await tool.execute(args if isinstance(args, dict) else {}, ToolContext(workspace="."))
        is_error = bool(result.metadata.get("error"))
        return _result(msg_id, {
            "content": [{"type": "text", "text": result.output}],
            "isError": is_error,
            "structuredContent": result.metadata,
        })

    if method == "shutdown":
        return None

    if msg_id is None:
        return None
    return _error(msg_id, -32601, f"Method not found: {method}")


def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def amain() -> None:
    tools = _tools()
    while True:
        raw = await asyncio.to_thread(sys.stdin.readline)
        if not raw:
            break
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if message.get("method") == "shutdown":
            break
        response = await _handle(message, tools)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
