"""Tool base — abstract contract, result types, context. No circular imports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from collections.abc import Callable

from pydantic import BaseModel, Field


def resolve_safe(workspace: str, file_path: str, extra_paths: list[str] | None = None) -> Path | None:
    """Resolve file path and verify it stays inside workspace (+ optional extra paths).

    Returns the resolved path if safe, or None if blocked.
    """
    ws = Path(workspace).resolve()
    resolved = (ws / file_path).resolve()

    allowed = [ws]
    if extra_paths:
        for ep in extra_paths:
            allowed.append(Path(ep).expanduser().resolve())

    for base in allowed:
        try:
            resolved.relative_to(base)
            return resolved
        except ValueError:
            continue
    return None


class ToolResult(BaseModel):
    """Result from tool execution. Typed so the agent can reason about it."""
    title: str = ""
    output: str
    metadata: dict = {}
    diff: str | None = None  # unified diff for edit/write tools


class ToolContext(BaseModel):
    """Context passed to every tool execution. Mutable file_mtimes for staleness guard."""
    workspace: str
    session_id: str = "default"
    agent: str = "build"
    file_mtimes: dict[str, float] = Field(default_factory=dict)
    mcp_manager: Any | None = None
    lsp_manager: Any | None = None
    sandbox_extra_paths: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class BaseTool(ABC):
    """Every tool has: id, description, typed parameters, deterministic execute."""

    id: str = ""
    description: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "id", ""):
            raise TypeError(f"{cls.__name__} must define a class attribute 'id'")
        if not getattr(cls, "description", ""):
            raise TypeError(f"{cls.__name__} must define a class attribute 'description'")

    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """Return JSON Schema dict generated from the tool's Pydantic input model."""
        ...

    @abstractmethod
    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        """Execute the tool with typed inputs. Returns typed result."""
        ...


SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".tox", ".eggs",
    ".idea", ".vscode", "dist", "build", "opencode",
    ".claude", ".ruff_cache",
})

SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".whl", ".egg",
})


def model_to_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model to JSON Schema dict."""
    schema = model.model_json_schema()
    return {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
        "additionalProperties": False,
    }
