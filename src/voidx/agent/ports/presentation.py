"""Narrow ports between agent use cases and presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.ports.input import InputFrontend


class TurnRunner(Protocol):
    async def run_turn(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
    ) -> None: ...


class SessionLifecycle(Protocol):
    async def restore_runtime_state(self) -> None: ...
    async def delete_empty_current_session(self) -> None: ...
    async def clear_current_session(self) -> None: ...




@dataclass(frozen=True)
class SessionPresentationStatus:
    session_id: str = ""
    title: str = "New session"
    directory: str = ""
    runtime_profile: str = "coding"
    is_new: bool = True


@dataclass(frozen=True)
class RuntimePresentationStatus:
    provider: str
    model: str
    workspace: str
    profile_configured: bool
    session: SessionPresentationStatus
    permission_mode: str = ""
    permission_label: str = "Safe"
    ai_approval_count: int = 0
    debug: bool = False
    plan_mode: bool = False
    interaction_mode: str = "auto"
    goal_label: str = ""
    active_workflows: tuple[str, ...] = ()
    reasoning_effort: str = "xhigh"
    protocol: str = ""
    context_window: int | None = None
    context_limit: int = 200_000
    mcp_config_path: str = ""
    code_ide: str = "trae"
    latest_action: str = ""


class RuntimeStatusReader(Protocol):
    def runtime_status(self) -> RuntimePresentationStatus: ...


class GuidancePort(Protocol):
    def can_submit_guidance(self) -> bool: ...
    def submit_guidance(self, text: str, **kwargs: Any) -> bool: ...


class InteractiveInputPort(Protocol):

    async def dispatch_input(
        self,
        user_input: str,
        *,
        context: TurnExecutionContext | None = None,
        thread_id: str = "",
    ) -> tuple[bool, str | None]: ...


class AgentEventPublisher(Protocol):
    def publish_message(self, message: str) -> None: ...
    def start_turn(self, text: str) -> None: ...
    def end_turn(self) -> None: ...
    def cancel_turn(self) -> None: ...
    def fail_turn(self, message: str) -> None: ...
    def show_loop_waiting(self, wakeup_at: float) -> None: ...
    def clear_loop_waiting(self) -> None: ...



class GatewayThreadRegistry(Protocol):
    def ensure_thread(self, session: SessionPresentationStatus) -> None: ...


class PresentationIntegrationLifecycle(Protocol):
    async def close_agent_gateway(self) -> None: ...
    async def stop_integrations(self) -> None: ...
    async def initialize_lsp(self) -> list[Any]: ...
    async def warm_up_lsp(self) -> dict[str, str]: ...
    def has_lsp(self) -> bool: ...
    def enabled_mcp_names(self) -> tuple[str, ...]: ...
    async def start_mcp(self) -> None: ...
    def has_mcp(self) -> bool: ...
    def mcp_statuses(self) -> list[Any]: ...
    def mcp_catalog(self) -> list[Any]: ...


class PresentationFrontendBinding(Protocol):
    def bind_input_frontend(self, frontend: InputFrontend | None) -> None: ...
    def reset_run_state(self) -> None: ...
    def bind_startup_presenter(self, presenter: Any) -> None: ...
    async def apply_settings_update(self, settings: object) -> None: ...
    def usage_stats(self) -> object: ...
    def update_check_due(self) -> bool: ...
    def mark_update_check(self, version: str | None) -> None: ...
    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool: ...

class PresentationSnapshotPort(Protocol):
    async def persist_current(self, session_id: str) -> None: ...
    async def restore_current(self, session_id: str, *, append: bool = False) -> bool: ...
    async def clear(self, session_id: str) -> None: ...


class NullPresentationSnapshotPort:
    async def persist_current(self, session_id: str) -> None:
        return None

    async def restore_current(self, session_id: str, *, append: bool = False) -> bool:
        return False

    async def clear(self, session_id: str) -> None:
        return None


class NullAgentEventPublisher:
    def publish_message(self, message: str) -> None:
        return None

    def start_turn(self, text: str) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def cancel_turn(self) -> None:
        return None

    def fail_turn(self, message: str) -> None:
        return None

    def show_loop_waiting(self, wakeup_at: float) -> None:
        return None

    def clear_loop_waiting(self) -> None:
        return None
