"""Validate gateway `call` arguments against the real MCP tool inputSchema.

Arguments must be an object. Validation distinguishes non-object input from
objects that violate the selected MCP tool's schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpArgumentError:
    kind: str  # "not_object" | "schema"
    message: str


@dataclass(frozen=True)
class McpValidatedArguments:
    arguments: dict[str, Any] | None
    error: McpArgumentError | None = None


def validate_mcp_arguments(raw: Any, input_schema: dict[str, Any]) -> McpValidatedArguments:
    parsed = _parse(raw)
    if isinstance(parsed, McpArgumentError):
        return McpValidatedArguments(arguments=None, error=parsed)

    schema = input_schema if isinstance(input_schema, dict) else {}
    if not schema:
        return McpValidatedArguments(arguments=parsed)

    error = _validate_schema(parsed, schema)
    if error is not None:
        return McpValidatedArguments(arguments=None, error=error)
    return McpValidatedArguments(arguments=parsed)


def _parse(raw: Any) -> dict[str, Any] | McpArgumentError:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return McpArgumentError(
        kind="not_object",
        message=f"arguments must be a JSON object, got {type(raw).__name__}.",
    )


def _validate_schema(arguments: dict[str, Any], schema: dict[str, Any]) -> McpArgumentError | None:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - dependency is declared
        return None
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(part) for part in e.absolute_path)
        location = f"field '{path}': " if path else ""
        return McpArgumentError(kind="schema", message=f"{location}{e.message}")
    except jsonschema.SchemaError:
        # A broken server-side schema must not block calls; the server validates anyway.
        return None
    return None
