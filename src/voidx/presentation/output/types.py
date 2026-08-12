"""Shared UI types — framework-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from voidx.agent.domain.turn_context import TurnExecutionContext

# Compatibility alias for TUI integrations using the former name.
ThreadExecutionContext = TurnExecutionContext


class UsageStatsView(Protocol):
    context_tokens: int
    context_limit: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_calls: int

    @property
    def total_tokens(self) -> int: ...

    @property
    def turn_calls(self) -> int: ...

    @property
    def turn_input_tokens(self) -> int: ...

    @property
    def turn_output_tokens(self) -> int: ...

    @property
    def cache_hit_rate(self) -> float | None: ...

    @property
    def cache_hit_rate_is_estimated(self) -> bool: ...

SubmitHandler = Callable[..., Awaitable[bool]]


def _status_value(status: Any, name: str) -> str:
    value = getattr(status, name, "")
    if callable(value):
        value = value()
    return str(value or "")


def _runtime_profile_for_status(status: Any):
    from voidx.agent.domain.profile import CHAT_PROFILE, CODING_PROFILE

    profile_id = _status_value(status, "runtime_profile") or "coding"
    if profile_id == "chat":
        return CHAT_PROFILE
    if profile_id == "goal":
        from voidx.agent.domain.automation.goal import GOAL_PROFILE

        return GOAL_PROFILE
    if profile_id == "loop":
        from voidx.agent.domain.automation.loop import LOOP_PROFILE

        return LOOP_PROFILE
    return CODING_PROFILE


def coding_turn_context_for_queue(
    status: Any,
    *,
    thread_id: str = "",
    context: TurnExecutionContext | None = None,
) -> TurnExecutionContext:
    from voidx.agent.domain.profile import CODING_PROFILE

    if context is not None:
        resolved_session_id = context.session_id
        resolved_thread_id = str(thread_id or context.thread_id or resolved_session_id or "coding")
        resolved_workspace = context.workspace
        profile = getattr(context, "runtime_profile", CODING_PROFILE)
    else:
        resolved_session_id = _status_value(status, "session_id")
        resolved_thread_id = str(thread_id or resolved_session_id or "coding")
        resolved_workspace = _status_value(status, "workspace")
        profile = _runtime_profile_for_status(status)
    if getattr(profile, "profile_id", "coding") == "coding":
        profile = CODING_PROFILE
    return TurnExecutionContext(
        thread_id=resolved_thread_id,
        session_id=resolved_session_id,
        runtime_profile=profile,
        workspace=resolved_workspace,
        tool_policy=getattr(context, "tool_policy", None),
    )


@dataclass
class McpServerStatus:
    name: str
    status: str = "configured"
    tool_count: int = 0
    source: str = "Project MCPs"


@dataclass
class UiStatus:
    provider: str
    model: str
    workspace: str
    session_title: str
    context_limit: int
    debug: Callable[[], bool]
    plan_mode: Callable[[], bool]
    interaction_mode: Callable[[], str] = field(default_factory=lambda: lambda: "auto")
    goal_label: Callable[[], str] = field(default_factory=lambda: lambda: "")
    active_workflows: Callable[[], list[str]] = field(default_factory=lambda: lambda: [])
    reasoning_effort: str = "xhigh"
    permission_label: Callable[[], str] = field(default_factory=lambda: lambda: "Safe")
    usage_stats: UsageStatsView | None = None
    mcp_servers: Callable[[], list[McpServerStatus]] = field(default_factory=lambda: lambda: [])
    mcp_config_path: str = ""
    code_ide: Callable[[], str] = field(default_factory=lambda: lambda: "trae")
    latest_action: Callable[[], str] = field(default_factory=lambda: lambda: "")
    runtime_profile: Callable[[], str] = field(default_factory=lambda: lambda: "coding")
    session_id: Callable[[], str] = field(default_factory=lambda: lambda: "")



class InteractionFrontend(Protocol):
    """Frontend interaction contract consumed by the core agent runtime."""

    @property
    def status(self) -> UiStatus: ...

    async def ask_choice(
        self,
        prompt: str,
        choices: list[str | tuple[str, str, str]],
        selected: int = 0,
        anchor: str = "",
        details: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> str | None: ...

    async def ask_text(
        self,
        prompt: str,
        default: str = "",
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None: ...

    async def run(self, on_submit: SubmitHandler) -> None: ...
    async def run_headless(self, on_submit: SubmitHandler) -> None: ...

    def submit_external_input(
        self,
        text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
    ) -> None: ...

    def cancel_external_input(
        self,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
    ) -> None: ...

    def set_external_command_handler(self, handler: Any) -> None: ...
    def set_external_request_handler(self, handler: Any) -> None: ...
    def invalidate(self) -> None: ...
    def consume_quiet_command(self, command: str) -> bool: ...
