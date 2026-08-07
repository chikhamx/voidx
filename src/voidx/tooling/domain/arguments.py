"""Argument normalization shared by Tooling plugins."""

from __future__ import annotations

from typing import Any


_NULLISH_TOOL_STRINGS = frozenset({"", "null", "none", "nil"})


def is_nullish_tool_value(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().lower() in _NULLISH_TOOL_STRINGS
    )


def normalize_nullable_tool_fields(args: dict[str, Any], *fields: str) -> dict[str, Any]:
    normalized = dict(args)
    for field in fields:
        if field in normalized and is_nullish_tool_value(normalized[field]):
            normalized[field] = None
    return normalized


def drop_nullish_tool_fields(args: dict[str, Any], *fields: str) -> dict[str, Any]:
    normalized = dict(args)
    for field in fields:
        if field in normalized and is_nullish_tool_value(normalized[field]):
            normalized.pop(field, None)
    return normalized


def keep_tool_args(args: Any, fields: set[str] | tuple[str, ...] | list[str]) -> Any:
    if not isinstance(args, dict):
        return args
    allowed = set(fields)
    return {key: value for key, value in args.items() if key in allowed}


__all__ = [
    "is_nullish_tool_value",
    "normalize_nullable_tool_fields",
    "drop_nullish_tool_fields",
    "keep_tool_args",
]
