"""@ file and # skill reference candidate JSON-RPC method handlers."""

from __future__ import annotations


class ReferenceMethods:
    """Reference candidate JSON-RPC handlers, mixed into GatewaySession."""

    def _workspace_for_thread(self, thread_id: str) -> str:
        info = self._threads.get(thread_id) if thread_id else None
        if info is not None and info.workspace:
            return info.workspace
        return self._workspace or "."

    def _method_attachments_save_image(self, params: dict) -> dict:
        import base64
        import binascii

        from voidx.presentation.protocol.v2.methods import MethodParamsError
        from voidx.presentation.tools.clipboard_image import save_clipboard_image_bytes

        data_base64 = params.get("data_base64")
        if not isinstance(data_base64, str) or not data_base64:
            raise MethodParamsError("data_base64 is required")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MethodParamsError("data_base64 is not valid base64") from exc

        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        workspace = self._workspace_for_thread(thread_id)
        result = save_clipboard_image_bytes(workspace, data)
        if not result.ok:
            return {"ok": False, "message": result.message}
        stem = result.rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return {
            "ok": True,
            "stem": stem,
            "rel_path": result.rel_path,
            "size": result.size,
            "compressed": result.compressed,
        }

    def _method_attachments_candidates(self, params: dict) -> dict:
        from voidx.presentation.tools.file_picker import list_file_candidates

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

    async def _method_skills_candidates(self, params: dict) -> dict:
        from voidx.presentation.protocol.v2.methods import MethodParamsError
        from voidx.presentation.tools.skill_picker import list_skill_candidates

        if self._skills_api_factory is None:
            raise MethodParamsError("skills_api_factory is required")
        query = str(params.get("query", "") or "")
        limit = int(params.get("limit", 8) or 8)
        thread_id = str(params.get("thread_id") or self._active_thread_id or "")
        workspace = self._workspace_for_thread(thread_id)
        skills_api = await self._skills_api_factory(workspace)
        candidates = list_skill_candidates(query, limit=limit, service=skills_api.service)
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
        from voidx.presentation.tools.mcp_picker import list_mcp_candidates

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
