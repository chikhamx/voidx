"""MCP server settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.models import McpServerConfig


class SettingsMcpMixin:
    def get_mcp_exposure(self) -> str:
        """How MCP tools are exposed to the model.

        Legacy direct/hybrid values are accepted in config files but ignored:
        MCP tools are exposed through the stable gateway tool only.
        """
        return "gateway"

    def list_mcp_servers(self) -> list[McpServerConfig]:
        data = self._effective_data()
        servers_data = data.get("mcpServers") or data.get("mcp_servers") or {}
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
        servers, _path, target = self._target_mapping("mcpServers")
        servers[server.name] = server.model_dump(exclude={"name"}, exclude_none=True)
        return self._save_target_mapping("mcpServers", servers, target)

    def delete_mcp_server(self, name: str) -> Path:
        servers, _path, target = self._target_mapping("mcpServers")
        if name in servers:
            servers.pop(name, None)
        elif self.get_mcp_server(name) is not None and target == "workspace":
            servers[name] = {"disabled": True}
        path = self._save_target_mapping("mcpServers", servers, target)
        self.clear_web_routes_for_server(name, save=True)
        return path

    def set_mcp_server_disabled(self, name: str, disabled: bool) -> Path:
        servers, _path, target = self._target_mapping("mcpServers")
        fields = servers.get(name)
        if not isinstance(fields, dict):
            effective_fields = self._mcp_servers_data().get(name)
            if not isinstance(effective_fields, dict):
                raise KeyError(name)
            fields = {} if target == "workspace" else effective_fields
        servers[name] = {**fields, "disabled": disabled}
        path = self._save_target_mapping("mcpServers", servers, target)
        if disabled:
            self.clear_web_routes_for_server(name, save=True)
        return path

    def set_mcp_server_auto(self, name: str, auto: bool) -> Path:
        servers, _path, target = self._target_mapping("mcpServers")
        fields = servers.get(name)
        if not isinstance(fields, dict):
            effective_fields = self._mcp_servers_data().get(name)
            if not isinstance(effective_fields, dict):
                raise KeyError(name)
            fields = {} if target == "workspace" else effective_fields
        servers[name] = {**fields, "auto": auto}
        return self._save_target_mapping("mcpServers", servers, target)

    def _mcp_servers_data(self) -> dict:
        data = self._effective_data()
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = data.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        return dict(servers)
