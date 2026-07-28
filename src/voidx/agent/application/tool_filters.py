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

    ``langchain-google-genai`` does not include keys such as
    ``additionalProperties`` and ``$schema`` in its ``_ALLOWED_SCHEMA_FIELDS``
    list, and the underlying google-genai ``Schema`` model only accepts string
    enum values.  For Gemini we strip incompatible fields recursively; other
    providers are unaffected.
    """
    if protocol != "gemini":
        return tool_defs

    stripped = copy.deepcopy(tool_defs)
    for tool in stripped:
        params = tool.get("function", {}).get("parameters")
        if isinstance(params, dict):
            _strip_gemini_unsupported_keys(params)
    return stripped


def _strip_gemini_unsupported_keys(schema: dict) -> None:
    """Recursively remove known unsupported Gemini schema keys in-place."""
    schema.pop("additionalProperties", None)
    schema.pop("$schema", None)
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and all(isinstance(v, str) for v in enum_values):
        non_empty_values = [v for v in enum_values if v]
        if non_empty_values:
            schema["enum"] = non_empty_values
        else:
            schema.pop("enum", None)
    else:
        schema.pop("enum", None)
    for value in schema.values():
        if isinstance(value, dict):
            _strip_gemini_unsupported_keys(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strip_gemini_unsupported_keys(item)
