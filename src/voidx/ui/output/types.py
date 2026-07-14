"""Shared UI types — framework-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from voidx.llm.usage import UsageStats

SubmitHandler = Callable[..., Awaitable[bool]]


@dataclass(frozen=True)
class ThreadExecutionContext:
    thread_id: str = ""
    session_id: str = ""


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
    sandbox_label: Callable[[], str] = field(default_factory=lambda: lambda: "w-write")
    approval_label: Callable[[], str] = field(default_factory=lambda: lambda: "on-fail")
    usage_stats: UsageStats = field(default_factory=UsageStats)
    mcp_servers: Callable[[], list[McpServerStatus]] = field(default_factory=lambda: lambda: [])
    mcp_config_path: str = ""
    code_ide: Callable[[], str] = field(default_factory=lambda: lambda: "trae")
    latest_action: Callable[[], str] = field(default_factory=lambda: lambda: "")


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
        context: ThreadExecutionContext | None = None,
    ) -> None: ...

    def cancel_external_input(
        self,
        *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> None: ...

    def set_external_command_handler(self, handler: Any) -> None: ...
    def set_external_request_handler(self, handler: Any) -> None: ...
    def invalidate(self) -> None: ...
    def consume_quiet_command(self, command: str) -> bool: ...
