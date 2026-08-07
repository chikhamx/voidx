"""Workspace-local cache for generated MCP server descriptions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from voidx.mcp.schema import McpToolDef
from voidx.platform.paths import voidx_workspace_dir

_CACHE_VERSION = 1
_CACHE_FILE_NAME = "mcp-descriptions.json"


class McpDescriptionCache:
    """Best-effort workspace cache that never affects MCP availability."""

    def __init__(self, workspace: str) -> None:
        self.path = voidx_workspace_dir(workspace) / _CACHE_FILE_NAME
        self._entries = self._load()

    def get(self, server: str, fingerprint: str) -> str | None:
        entry = self._entries.get(server)
        if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
            return None
        description = entry.get("description")
        return description.strip() if isinstance(description, str) and description.strip() else None

    def put(self, server: str, fingerprint: str, description: str) -> None:
        description = description.strip()
        if not description:
            return
        self._entries[server] = {
            "fingerprint": fingerprint,
            "description": description,
        }
        self._save()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            return {}
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {
            str(name): value
            for name, value in entries.items()
            if isinstance(name, str) and isinstance(value, dict)
        }

    def _save(self) -> None:
        payload = {
            "version": _CACHE_VERSION,
            "entries": self._entries,
        }
        temp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp, self.path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def description_fingerprint(server: Any, tools: list[McpToolDef]) -> str:
    server_data = {
        key: getattr(server, key, None)
        for key in ("name", "command", "args", "url", "cwd", "headers", "env", "tools", "transport")
    }
    tool_data = [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    payload = json.dumps(
        {"server": server_data, "tools": tool_data},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
