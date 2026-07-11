"""Tool base — abstract contract, result types, context. No circular imports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from voidx.workflow.types import WorkflowRunState


def tool_timeout_metadata(source: str, **extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "error": True,
        "timeout": True,
        "error_kind": "tool_timeout",
        "timeout_source": source,
    }


def resolve_safe(workspace: str, file_path: str, extra_paths: list[str] | None = None) -> Path | None:
    """Resolve file path and verify it stays inside workspace (+ optional extra paths).

    Returns the resolved path if safe, or None if blocked.
    """
    ws = Path(workspace).resolve()
    raw = Path(file_path)
    if file_path.startswith("~") or raw.is_absolute():
        resolved = raw.expanduser().resolve()
    else:
        resolved = (ws / raw).resolve()

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
    summary: str = ""
    metadata: dict = {}
    diff: str | None = None  # unified diff for edit/write tools
    next_step_hint: str = ""
    display: str = ""  # human-readable format for UI; LLM sees output


class UserInteraction(BaseModel):
    """A request for user input from a tool.

    options format determines routing:
    - list[str]: clarify-style, routed to ask_text (free text with suggestions)
    - list[tuple[str, str, str]]: choice-style, routed to ask_choice (label, value, desc)
    """

    prompt: str
    options: list[str | tuple[str, str, str]] = Field(default_factory=list)
    timeout: float | None = None


class UserResponse(BaseModel):
    """The user's response to a user interaction."""

    value: str
    cancelled: bool = False
    free_text: bool = False


UserInteractionCallback = Callable[[UserInteraction], Awaitable[UserResponse]]
AddExtraPathCallback = Callable[[str], None]


class ToolContext(BaseModel):
    """Context passed to every tool execution. Mutable file fingerprints for staleness guard."""

    workspace: str
    session_id: str = "default"
    persona: str = "voidx"
    interaction_mode: str = "auto"
    task_intent: str = "coding"
    goal_type: str = ""
    goal_target: str = ""
    turn_count: int = 0
    active_workflow_names: list[str] = Field(default_factory=list)
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)
    workflow_route: dict[str, str | None] | None = None
    mcp_manager: Any | None = None
    lsp_manager: Any | None = None
    sandbox_mode: str = "workspace-write"
    sandbox_extra_paths: list[str] = Field(default_factory=list)
    interact: UserInteractionCallback | None = Field(default=None, exclude=True)
    add_extra_path: AddExtraPathCallback | None = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def __init__(self, **data: Any) -> None:
        # Pydantic v2 deep-copies dict fields, breaking reference sharing
        # with the host.  Store these as private attributes so mutations
        # propagate across tool calls via the shared host dicts.
        fm = data.pop("file_mtimes", None)
        if fm is None:
            fm = {}
        frc = data.pop("file_read_coverage", None)
        if frc is None:
            frc = {}
        wrt = data.pop("workflow_repeat_tracker", None)
        if wrt is None:
            wrt = {}
        super().__init__(**data)
        self._file_mtimes = fm
        self._file_read_coverage = frc
        self._workflow_repeat_tracker = wrt

    @property
    def file_mtimes(self) -> dict[str, dict[str, int]]:
        return self._file_mtimes

    @property
    def file_read_coverage(self) -> dict[str, dict[str, Any]]:
        return self._file_read_coverage

    @property
    def workflow_repeat_tracker(self) -> dict[str, dict[str, int]]:
        return self._workflow_repeat_tracker


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
    """Convert a Pydantic model to JSON Schema dict.

    All properties are marked required so the schema complies with OpenAI
    strict mode and strict third-party proxies that validate ``required``
    must list every key in ``properties``.  Optional fields use ``default``
    or ``defaultFactory`` in their definition so the LLM can omit them.
    """
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
    _disallow_extra_properties(result)
    return result



def _inline_refs(schema: dict[str, Any], defs: dict[str, Any]) -> None:
    """Replace all $ref nodes with inlined copies from $defs, preserving sibling keys like description."""
    for key, value in list(schema.items()):
        if isinstance(value, dict):
            if "$ref" in value:
                ref_name = value.pop("$ref").rsplit("/", 1)[-1]
                inlined = dict(defs[ref_name])
                inlined.update({k: v for k, v in value.items() if k != "$ref"})
                schema[key] = inlined
                _inline_refs(inlined, defs)
            else:
                _inline_refs(value, defs)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    if "$ref" in item:
                        ref_name = item.pop("$ref").rsplit("/", 1)[-1]
                        inlined = dict(defs[ref_name])
                        inlined.update({k: v for k, v in item.items() if k != "$ref"})
                        value[i] = inlined
                        _inline_refs(inlined, defs)
                    else:
                        _inline_refs(item, defs)


def _disallow_extra_properties(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
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
