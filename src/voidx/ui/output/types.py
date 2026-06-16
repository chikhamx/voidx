"""Shared UI types — framework-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from voidx.llm.usage import UsageStats

SubmitHandler = Callable[[str], Awaitable[bool]]


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
    goal_type: Callable[[], str] = field(default_factory=lambda: lambda: "")
    goal_awaiting_approval: Callable[[], bool] = field(default_factory=lambda: lambda: False)
    active_workflows: Callable[[], list[str]] = field(default_factory=lambda: lambda: [])
    reasoning_effort: str = "xhigh"
    permission_label: Callable[[], str] = field(default_factory=lambda: lambda: "default")
    sandbox_label: Callable[[], str] = field(default_factory=lambda: lambda: "w-write")
    approval_label: Callable[[], str] = field(default_factory=lambda: lambda: "on-fail")
    approval_reviewer_label: Callable[[], str] = field(default_factory=lambda: lambda: "user")
    usage_stats: UsageStats = field(default_factory=UsageStats)
    mcp_servers: Callable[[], list[McpServerStatus]] = field(default_factory=lambda: lambda: [])
    mcp_config_path: str = ""
    code_ide: Callable[[], str] = field(default_factory=lambda: lambda: "trae")
    latest_action: Callable[[], str] = field(default_factory=lambda: lambda: "")
