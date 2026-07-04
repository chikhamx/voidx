"""Tool definition filters shared by primary and worker-persona loops."""

from __future__ import annotations

import copy
from typing import Any


def filter_unavailable_lsp_tools(tool_defs: list[dict], lsp_manager: Any | None) -> list[dict]:
    if _has_available_lsp_server(lsp_manager):
        return tool_defs
    return [
        tool
        for tool in tool_defs
        if not str(tool.get("function", {}).get("name", "")).startswith("lsp")
    ]


def _has_available_lsp_server(lsp_manager: Any | None) -> bool:
    if lsp_manager is None or not hasattr(lsp_manager, "has_available_server"):
        return False
    try:
        return bool(lsp_manager.has_available_server())
    except Exception:
        return False


def strip_gemini_unsupported_schema_keys(
    tool_defs: list[dict], protocol: str | None
) -> list[dict]:
    """Remove schema keys that langchain-google-genai warns about.

    ``langchain-google-genai`` does not include ``additionalProperties`` in its
    ``_ALLOWED_SCHEMA_FIELDS`` list, so every tool schema that contains it
    emits a warning per LLM call.  For Gemini we strip it recursively; other
    providers are unaffected.
    """
    if protocol != "gemini":
        return tool_defs

    stripped = copy.deepcopy(tool_defs)
    for tool in stripped:
        params = tool.get("function", {}).get("parameters")
        if isinstance(params, dict):
            _strip_additional_properties(params)
    return stripped


def _strip_additional_properties(schema: dict) -> None:
    """Recursively remove ``additionalProperties`` from a schema dict in-place."""
    schema.pop("additionalProperties", None)
    for value in schema.values():
        if isinstance(value, dict):
            _strip_additional_properties(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strip_additional_properties(item)
