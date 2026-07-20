"""McpClient preserves serverInfo and instructions from the initialize handshake."""

import json
import sys

import pytest

from voidx.config import Settings
from voidx.mcp.manager import McpManager
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry


_FAKE_SERVER = """
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        params = message.get("params", {})
        result = {
            "protocolVersion": params.get("protocolVersion"),
            "capabilities": {},
            "serverInfo": {"name": "fake-server", "version": "2.1.0"},
            "instructions": "Web search and content extraction tools.",
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
            ]
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
"""


def _write_env(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(_FAKE_SERVER, encoding="utf-8")
    (tmp_path / "voidx.json").write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(server)]}}}),
        encoding="utf-8",
    )
    return Settings(str(tmp_path))


@pytest.mark.asyncio
async def test_catalog_entry_carries_server_info_and_instructions(tmp_path):
    settings = _write_env(tmp_path)
    manager = McpManager(settings, ToolRegistry(settings=settings), PermissionService())

    await manager.start_all()
    await manager.wait_ready()
    try:
        entries = {e.name: e for e in manager.catalog_snapshot()}
        assert "fake" in entries
        entry = entries["fake"]
        assert entry.server_info == {"name": "fake-server", "version": "2.1.0"}
        assert entry.instructions == "Web search and content extraction tools."
    finally:
        await manager.stop_all()
