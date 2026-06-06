"""Web tool routing settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.models import WebToolRoute


class SettingsWebMixin:
    def get_web_tool_route(self, kind: str) -> WebToolRoute:
        web = self._data.get("web", {})
        if not isinstance(web, dict):
            return WebToolRoute()
        fields = web.get(kind, {})
        if not isinstance(fields, dict):
            return WebToolRoute()
        try:
            return WebToolRoute(**fields)
        except ValueError:
            return WebToolRoute()

    def set_web_tool_route(self, kind: str, route: WebToolRoute) -> Path:
        web = self._data.get("web", {})
        if not isinstance(web, dict):
            web = {}
        web[kind] = route.model_dump()
        self._data["web"] = web
        self._save()
        return self._path

    def clear_web_routes_for_server(self, server: str) -> None:
        web = self._data.get("web", {})
        if not isinstance(web, dict):
            return
        for kind, fields in list(web.items()):
            if isinstance(fields, dict) and fields.get("server") == server:
                web[kind] = WebToolRoute().model_dump()
