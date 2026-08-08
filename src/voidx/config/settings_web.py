"""Web tool routing settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.tooling.domain.web import WebToolRoute


class SettingsWebMixin:
    def get_web_tool_route(self, kind: str) -> WebToolRoute:
        web = self._effective_data().get("web", {})
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
        web, _path, target = self._target_mapping("web")
        web[kind] = route.model_dump()
        return self._save_target_mapping("web", web, target)

    def clear_web_routes_for_server(self, server: str, *, save: bool = False) -> Path | None:
        if save:
            web, _path, target = self._target_mapping("web")
        else:
            web = self._effective_data().get("web", {})
        if not isinstance(web, dict):
            return None
        web = dict(web)
        changed = False
        for kind, fields in list(web.items()):
            if isinstance(fields, dict) and fields.get("server") == server:
                web[kind] = WebToolRoute().model_dump()
                changed = True
        if save:
            return self._save_target_mapping("web", web, target) if changed else self.path
        if changed:
            data, _path, _target = self._write_target("web")
            data["web"] = web
            self._effective_cache = None
        return None
