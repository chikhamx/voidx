"""@ file and # skill reference candidate JSON-RPC method handlers."""

from __future__ import annotations


class ReferenceMethods:
    """Reference candidate JSON-RPC handlers, mixed into GatewaySession."""

    def _workspace_for_thread(self, thread_id: str) -> str:
        info = self._threads.get(thread_id) if thread_id else None
        if info is not None and info.workspace:
            return info.workspace
        return self._workspace or "."

    def _method_attachments_candidates(self, params: dict) -> dict:
        from voidx.ui.tools.file_picker import list_file_candidates

        query = str(params.get("query", "") or "")
        limit = int(params.get("limit", 8) or 8)
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        workspace = self._workspace_for_thread(thread_id)
        candidates = list_file_candidates(workspace, query, limit=limit)
        return {
            "candidates": [
                {
                    "rel_path": c.rel_path,
                    "kind": c.kind,
                    "size": c.size,
                }
                for c in candidates
            ]
        }

    def _method_skills_candidates(self, params: dict) -> dict:
        from voidx.ui.tools.skill_picker import list_skill_candidates

        query = str(params.get("query", "") or "")
        limit = int(params.get("limit", 8) or 8)
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        workspace = self._workspace_for_thread(thread_id)
        candidates = list_skill_candidates(workspace, query, limit=limit)
        return {
            "candidates": [
                {
                    "name": c.name,
                    "scope": c.scope,
                    "description": c.description,
                    "mode": c.mode,
                }
                for c in candidates
            ]
        }

    def _method_mcp_candidates(self, params: dict) -> dict:
        from voidx.ui.tools.mcp_picker import list_mcp_candidates

        query = str(params.get("query", "") or "")
        limit = int(params.get("limit", 8) or 8)
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        workspace = self._workspace_for_thread(thread_id)
        settings = self._gateway_settings()
        catalog = self._mcp_catalog_provider() if self._mcp_catalog_provider else None
        candidates = list_mcp_candidates(
            workspace, query, limit=limit, settings=settings, catalog=catalog,
        )
        return {
            "candidates": [
                {
                    "name": c.name,
                    "description": c.description,
                    "mode": c.mode,
                }
                for c in candidates
            ]
        }
