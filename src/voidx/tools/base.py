"""Tool base — abstract contract, result types, context. No circular imports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from voidx.workflow.types import WorkflowRunState


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


class UserInteraction(BaseModel):
    """A request for user input from a tool."""
    prompt: str
    options: list[tuple[str, str, str]] = Field(default_factory=list)
    blocking: bool = True
    timeout: float | None = None


class UserResponse(BaseModel):
    """The user's response to a tool interaction request."""
    value: str
    cancelled: bool = False
    free_text: bool = False


UserInteractionCallback = Callable[[UserInteraction], Awaitable[UserResponse]]


class ToolContext(BaseModel):
    """Context passed to every tool execution. Mutable file_mtimes for staleness guard."""
    workspace: str
    session_id: str = "default"
    persona: str = "voidx"
    interaction_mode: str = "auto"
    task_intent: str = "coding"
    pending_approval: dict | None = None
    goal_type: str = ""
    goal_target: str = ""
    active_workflow_names: list[str] = Field(default_factory=list)
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)
    file_mtimes: dict[str, float] = Field(default_factory=dict)
    mcp_manager: Any | None = None
    lsp_manager: Any | None = None
    sandbox_mode: str = "workspace-write"
    sandbox_extra_paths: list[str] = Field(default_factory=list)
    interact: UserInteractionCallback | None = Field(default=None, exclude=True)

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
    result = {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
        "additionalProperties": False,
    }
    if "$defs" in schema:
        result["$defs"] = schema["$defs"]
    _disallow_extra_properties(result)
    return result


def _disallow_extra_properties(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    for value in schema.values():
        if isinstance(value, dict):
            _disallow_extra_properties(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _disallow_extra_properties(item)
