"""Tool base — abstract contract, result types, context. No circular imports."""

from __future__ import annotations

from abc import ABC, abstractmethod
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from voidx.permission.grants import (
    AccessGrant,
    AccessGrants,
    ApprovalPrecondition,
    GrantPersistence,
    ObjectType,
    grant_for_intent,
    resolve_access,
)
from voidx.workflow.types import WorkflowRunState
from voidx.paths import resolve_tool_path as _resolve_tool_path


def tool_timeout_metadata(source: str, **extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "error": True,
        "timeout": True,
        "error_kind": "tool_timeout",
        "timeout_source": source,
    }


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
    return {key: value for key, value in args.items() if key in set(fields)}


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



class ApprovedToolRisk(BaseModel):
    """One approval token for one exact tool invocation."""

    tool_name: str
    pattern: str = ""
    risk_level: str = ""
    tags: tuple[str, ...] = ()
    reason: str = ""
    approved_by: Literal["user", "ai", "cached"] = "user"

UserInteractionCallback = Callable[[UserInteraction], Awaitable[UserResponse]]


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
    format_after_edit_enabled: bool = True
    loop_controller: Any | None = Field(default=None, exclude=True)
    goal_controller: Any | None = Field(default=None, exclude=True)
    goal_intake_controller: Any | None = Field(default=None, exclude=True)
    goal_phase: str = "work"
    loop_intake_controller: Any | None = Field(default=None, exclude=True)
    loop_phase: str = "work"
    tool_registry: Any | None = Field(default=None, exclude=True)
    agent_gateway: Any | None = Field(default=None, exclude=True)
    agent_run_id: str = Field(default="", exclude=True)
    permission_mode: str = "safe"
    sandbox_readable_files: list[str] = Field(default_factory=list)
    sandbox_readable_dirs: list[str] = Field(default_factory=list)
    sandbox_writable_files: list[str] = Field(default_factory=list)
    sandbox_writable_dirs: list[str] = Field(default_factory=list)
    get_access_grants: Callable[[], AccessGrants] | None = Field(default=None, exclude=True)
    get_revocation_epoch: Callable[[], int] | None = Field(default=None, exclude=True)
    add_grant: Callable[..., Awaitable[Any] | Any] | None = Field(default=None, exclude=True)
    acquire_grant_targets: Callable[..., Awaitable[Any]] | None = Field(default=None, exclude=True)
    acquire_execution_lease: Callable[[str], Any] | None = Field(default=None, exclude=True)
    process_sandbox: Any | None = Field(default=None, exclude=True)
    interact: UserInteractionCallback | None = Field(default=None, exclude=True)
    approved_tool_risks: list[ApprovedToolRisk] = Field(default_factory=list)

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

    @property
    def sandbox_mode(self) -> str:
        from voidx.config import PermissionMode
        try:
            return PermissionMode(self.permission_mode).sandbox_mode
        except ValueError:
            return "workspace-write"

    @property
    def approval_policy(self) -> str:
        from voidx.config import PermissionMode
        try:
            return PermissionMode(self.permission_mode).approval_policy
        except ValueError:
            return "untrusted"

    def has_approved_tool_risk(self, tool_name: str, pattern: str) -> bool:
        for risk in self.approved_tool_risks:
            if risk.tool_name == tool_name and risk.pattern == pattern and risk.risk_level != "blocked":
                return True
        return False




async def _resolve_tool_path_for_access(
    ctx: ToolContext,
    file_path: str,
    *,
    write: bool,
    require_exists: bool = False,
    allow_missing_write_file: bool = False,
    prompt_label: str | None = None,
    allow_description: str | None = None,
    deny_description: str | None = None,
) -> tuple[Path | None, ToolResult | None]:
    access = "write" if write else "read"
    access_grants = ctx.get_access_grants() if ctx.get_access_grants is not None else AccessGrants.from_parts(
        readable_files=ctx.sandbox_readable_files,
        readable_dirs=ctx.sandbox_readable_dirs,
        writable_files=ctx.sandbox_writable_files,
        writable_dirs=ctx.sandbox_writable_dirs,
    )
    precondition = _approval_precondition(access_grants)
    resolution = resolve_access(
        ctx.workspace,
        file_path,
        access=access,
        access_grants=access_grants,
        require_exists=require_exists,
        allow_missing_write_file=allow_missing_write_file,
    )
    if resolution.action == "allow" and resolution.intent is not None:
        return resolution.intent.normalized_path, None
    if resolution.action == "deny":
        return None, ToolResult(output=resolution.reason or f"Path traversal blocked: {file_path}", metadata={"error": True})

    label = prompt_label or ("Write" if write else "Read")
    if not ctx.interact:
        return None, ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})
    lock = None
    if ctx.acquire_grant_targets is not None and resolution.intent is not None:
        lock = await ctx.acquire_grant_targets([resolution.intent.normalized_path])
        access_grants = ctx.get_access_grants() if ctx.get_access_grants is not None else access_grants
        precondition = _approval_precondition(access_grants)
        resolution = resolve_access(
            ctx.workspace,
            file_path,
            access=access,
            access_grants=access_grants,
            require_exists=require_exists,
            allow_missing_write_file=allow_missing_write_file,
        )
        if resolution.action == "allow" and resolution.intent is not None:
            await _release_lock(lock)
            return resolution.intent.normalized_path, None
        if resolution.action == "deny":
            await _release_lock(lock)
            return None, ToolResult(output=resolution.reason or f"Path traversal blocked: {file_path}", metadata={"error": True})
    try:
        options = [
            ("Yes", "allow", allow_description or f"Allow this {access} once"),
            ("No", "deny", deny_description or f"Do not {access} this file"),
        ]
        if ctx.add_grant is not None:
            options = [
                ("Session file", "session_file", allow_description or f"Allow this {access} file for this session"),
                ("Session dir", "session_dir", f"Allow this {access} directory for this session"),
                ("Persistent file", "persistent_file", f"Always allow this {access} file"),
                ("Persistent dir", "persistent_dir", f"Always allow this {access} directory"),
                ("Once", "allow", f"Allow this {access} once"),
                ("No", "deny", deny_description or f"Do not {access} this file"),
            ]
        response = await ctx.interact(UserInteraction(
            prompt=f"{label} file outside workspace? {file_path}",
            options=options,
        ))
        if response.cancelled or response.value == "deny":
            return None, ToolResult(output=f"{label} denied by user: {file_path}", metadata={"error": True})
        if resolution.intent is None:
            return None, ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})
        if response.value in {"session_file", "session_dir", "persistent_file", "persistent_dir"} and ctx.add_grant is not None:
            persistence: GrantPersistence = "persistent" if response.value.startswith("persistent") else "session"
            object_type: ObjectType = "dir" if response.value.endswith("dir") else "file"
            grant = grant_for_intent(resolution.intent, persistence, object_type=object_type)
            if ctx.acquire_grant_targets is not None:
                await _release_lock(lock)
                lock = await ctx.acquire_grant_targets([resolution.intent.normalized_path], final_paths=[grant.path])
                access_grants = ctx.get_access_grants() if ctx.get_access_grants is not None else access_grants
                precondition = _approval_precondition(access_grants)
                resolution = resolve_access(
                    ctx.workspace,
                    file_path,
                    access=access,
                    access_grants=access_grants,
                    require_exists=require_exists,
                    allow_missing_write_file=allow_missing_write_file,
                )
                if resolution.action == "allow" and resolution.intent is not None:
                    return resolution.intent.normalized_path, None
                if resolution.action == "deny":
                    return None, ToolResult(output=resolution.reason or f"Path traversal blocked: {file_path}", metadata={"error": True})
            result = await _call_add_grant(ctx.add_grant, grant, precondition)
            if getattr(result, "ok", True) is False:
                message = getattr(result, "error", "Permission grant conflict") or "Permission grant conflict"
                return None, ToolResult(output=message, metadata={"error": True, "conflict": getattr(result, "conflict", False)})
        return resolution.intent.normalized_path, None
    finally:
        await _release_lock(lock)


async def _release_lock(lock: Any | None) -> None:
    if lock is not None and hasattr(lock, "release"):
        await _maybe_await(lock.release())


async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value



def _approval_precondition(access_grants: AccessGrants) -> ApprovalPrecondition:
    return ApprovalPrecondition(
        permission_mode=access_grants.permission_mode,
        revocation_epoch=access_grants.revocation_epoch,
    )


async def _call_add_grant(add_grant: Callable[..., Any], grant: AccessGrant, precondition: ApprovalPrecondition) -> Any:
    try:
        signature = inspect.signature(add_grant)
        accepts_precondition = (
            "precondition" in signature.parameters
            or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        )
    except (TypeError, ValueError):
        accepts_precondition = True
    if accepts_precondition:
        return await _maybe_await(add_grant(grant, precondition=precondition))
    return await _maybe_await(add_grant(grant))


def _sandbox_paths_for_access(ctx: ToolContext, *, write: bool) -> list[str]:
    writable = [*ctx.sandbox_writable_files, *ctx.sandbox_writable_dirs]
    if write:
        return writable
    return [
        *ctx.sandbox_readable_files,
        *ctx.sandbox_readable_dirs,
        *writable,
    ]


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
    _flatten_anyof(result)
    _disallow_extra_properties(result)
    return result


def _flatten_anyof(schema: dict[str, Any]) -> None:
    """Replace anyOf with a multi-type 'type' array for OpenAI strict mode.

    Pydantic generates ``anyOf`` for ``str | list[str] | None`` fields.
    OpenAI strict mode handles multi-type ``type`` arrays more reliably
    than ``anyOf``, especially when the field is also in ``required``.
    """
    for key, value in list(schema.items()):
        if isinstance(value, dict):
            if "anyOf" in value:
                _replace_anyof(value)
            _flatten_anyof(value)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    if "anyOf" in item:
                        _replace_anyof(item)
                    _flatten_anyof(item)


def _replace_anyof(prop: dict[str, Any]) -> None:
    """Convert an anyOf node into a flat type array, preserving siblings."""
    branches = prop.pop("anyOf")
    types: list[str] = []
    array_items: dict[str, Any] | None = None
    for branch in branches:
        t = branch.get("type")
        if t == "array":
            types.append("array")
            array_items = branch.get("items")
        elif t is not None:
            types.append(t)
    if not types:
        return
    if len(types) == 1:
        prop["type"] = types[0]
    else:
        prop["type"] = types
    if "array" in types and array_items is not None:
        prop["items"] = array_items


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
