"""MCP server settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.models import McpServerConfig


class SettingsMcpMixin:
    def list_mcp_servers(self) -> list[McpServerConfig]:
        servers_data = self._data.get("mcpServers") or self._data.get("mcp_servers") or {}
        if not isinstance(servers_data, dict):
            return []

        result: list[McpServerConfig] = []
        for name, fields in servers_data.items():
            if not isinstance(fields, dict):
                continue
            try:
                result.append(McpServerConfig(name=name, **fields))
            except ValueError:
                continue
        return result

    def get_mcp_server(self, name: str) -> McpServerConfig | None:
        for server in self.list_mcp_servers():
            if server.name == name:
                return server
        return None

    def save_mcp_server(self, server: McpServerConfig) -> Path:
        servers = self._mcp_servers_data()
        servers[server.name] = server.model_dump(exclude={"name"}, exclude_none=True)
        self._data["mcpServers"] = servers
        self._save()
        return self._path

    def delete_mcp_server(self, name: str) -> Path:
        servers = self._mcp_servers_data()
        servers.pop(name, None)
        self._data["mcpServers"] = servers
        self.clear_web_routes_for_server(name)
        self._save()
        return self._path

    def set_mcp_server_disabled(self, name: str, disabled: bool) -> Path:
        servers = self._mcp_servers_data()
        fields = servers.get(name)
        if not isinstance(fields, dict):
            raise KeyError(name)
        servers[name] = {**fields, "disabled": disabled}
        self._data["mcpServers"] = servers
        if disabled:
            self.clear_web_routes_for_server(name)
        self._save()
        return self._path

    def _mcp_servers_data(self) -> dict:
        servers = self._data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = self._data.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        return dict(servers)
