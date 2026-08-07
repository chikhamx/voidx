"""JSON schema normalization for strict tool catalogs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".tox", ".eggs", ".idea", ".vscode", "dist", "build",
    "opencode", ".claude", ".ruff_cache",
})
SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".pdf", ".zip", ".tar", ".gz", ".whl", ".egg",
})


def model_to_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    properties = schema.get("properties", {})
    result = {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }
    if defs:
        _inline_refs(result, defs)
    _flatten_anyof(result)
    _disallow_extra_properties(result)
    return result


def _flatten_anyof(schema: dict[str, Any]) -> None:
    for value in list(schema.values()):
        if isinstance(value, dict):
            if "anyOf" in value:
                _replace_anyof(value)
            _flatten_anyof(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    if "anyOf" in item:
                        _replace_anyof(item)
                    _flatten_anyof(item)


def _replace_anyof(prop: dict[str, Any]) -> None:
    branches = prop.pop("anyOf")
    types: list[str] = []
    array_items: dict[str, Any] | None = None
    for branch in branches:
        kind = branch.get("type")
        if kind == "array":
            types.append(kind)
            array_items = branch.get("items")
        elif kind is not None:
            types.append(kind)
    if not types:
        return
    prop["type"] = types[0] if len(types) == 1 else types
    if array_items is not None:
        prop["items"] = array_items


def _inline_refs(schema: dict[str, Any], defs: dict[str, Any]) -> None:
    for key, value in list(schema.items()):
        if isinstance(value, dict):
            if "$ref" in value:
                ref_name = value.pop("$ref").rsplit("/", 1)[-1]
                inlined = dict(defs[ref_name])
                inlined.update(value)
                schema[key] = inlined
                _inline_refs(inlined, defs)
            else:
                _inline_refs(value, defs)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    if "$ref" in item:
                        ref_name = item.pop("$ref").rsplit("/", 1)[-1]
                        inlined = dict(defs[ref_name])
                        inlined.update(item)
                        value[index] = inlined
                        _inline_refs(inlined, defs)
                    else:
                        _inline_refs(item, defs)


def _disallow_extra_properties(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        props = schema.get("properties")
        if props:
            schema["required"] = list(props.keys())
    for value in schema.values():
        if isinstance(value, dict):
            _disallow_extra_properties(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _disallow_extra_properties(item)


__all__ = ["SKIP_DIRS", "SKIP_SUFFIXES", "model_to_json_schema"]
