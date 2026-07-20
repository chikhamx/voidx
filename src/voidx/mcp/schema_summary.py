"""Compress MCP tool input schemas into readable field summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_FIELDS = 12


@dataclass(frozen=True)
class McpFieldSummary:
    name: str
    required: bool
    type: str
    description: str = ""
    enum: list[Any] | None = None


@dataclass(frozen=True)
class McpSchemaSummary:
    fields: tuple[McpFieldSummary, ...] = field(default_factory=tuple)
    truncated: bool = False

    @property
    def required_names(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    @property
    def optional_names(self) -> list[str]:
        return [f.name for f in self.fields if not f.required]


def summarize_schema(input_schema: dict[str, Any], *, max_fields: int = DEFAULT_MAX_FIELDS) -> McpSchemaSummary:
    if not isinstance(input_schema, dict):
        return McpSchemaSummary()

    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return McpSchemaSummary()

    required_raw = input_schema.get("required")
    required = set(required_raw) if isinstance(required_raw, list) else set()

    fields = [
        McpFieldSummary(
            name=name,
            required=name in required,
            type=_type_label(spec),
            description=str(spec.get("description", "")) if isinstance(spec, dict) else "",
            enum=list(spec["enum"]) if isinstance(spec, dict) and isinstance(spec.get("enum"), list) else None,
        )
        for name, spec in properties.items()
        if isinstance(name, str)
    ]
    fields.sort(key=lambda f: (not f.required, f.name))

    truncated = len(fields) > max_fields
    return McpSchemaSummary(fields=tuple(fields[:max_fields]), truncated=truncated)


def _type_label(spec: Any) -> str:
    if not isinstance(spec, dict):
        return "any"
    base = spec.get("type")
    if isinstance(base, list):
        base = next((t for t in base if t != "null"), base[0] if base else "any")
    if not isinstance(base, str):
        if "enum" in spec:
            return "enum"
        return "any"
    if base == "array":
        items = spec.get("items")
        item_type = items.get("type", "any") if isinstance(items, dict) else "any"
        return f"array[{item_type}]"
    return base
