"""Integration JSON-RPC method handlers (mcp/skills/lsp/tavily) for GatewaySession."""

from __future__ import annotations

from voidx.logging import log_internal_error
from voidx.ui.protocol.v2.methods import MethodParamsError


class IntegrationMethods:
    """MCP/skills/LSP/tavily JSON-RPC handlers, mixed into GatewaySession."""

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
        settings = self._settings_for_scope(scope, self._workspace or ".")
        settings.set_tavily_api_key(api_key.strip())
        return {"ok": True, "tavily": self._tavily_summary(settings)}

    def _method_tavily_delete(self, params: dict) -> dict:
        scope = params.get("scope", "global")
        if scope not in {"global", "workspace"}:
            raise MethodParamsError("invalid scope")
        settings = self._settings_for_scope(scope, self._workspace or ".")
        settings.delete_tavily_api_key()
        return {"ok": True, "tavily": self._tavily_summary(settings)}

    def _skill_service(self, settings):
        from voidx.skills.service import SkillService
        return SkillService.for_workspace(self._workspace or ".", selection=settings.get_skill_selection())

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
        except Exception as exc:
            log_internal_error(exc, context="gateway_lsp_manager_init")
        return manager

    async def _lsp_status_list(self) -> list[dict]:
        manager = await self._new_lsp_manager()
        return [status.model_dump() for status in manager.statuses()]
