"""Per-thread execution state isolation for graph turns."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.tool_policy import ToolPolicy
from voidx.agent.domain.turn_context import TurnExecutionContext

from voidx.agent.adapters.langgraph.runtime.runtime_guards import RuntimeGuardState
from voidx.agent.adapters.langgraph.runtime.topology import session_date
from voidx.agent.application.runtime_context import ContextCompilerCache, InteractionMode
from voidx.agent.domain.task.state import TaskState
from voidx.agent.adapters.persistence.session_repository import SessionInfo, get_session
from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state
from voidx.platform.execution_context import ExecutionIdentity, bind_execution_identity

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage



@dataclass
class GuidanceEntry:
    text: str
    truncated: bool = False
    source: Literal["user", "guard"] = "user"
    thread_id: str = ""
    session_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

@dataclass
class ThreadExecutionState:
    """Mutable graph state that must not bleed across concurrent turns."""

    thread_id: str = ""
    session: SessionInfo | None = None
    session_msg_cache: list[BaseMessage] | None = None
    context_cache: ContextCompilerCache = field(default_factory=ContextCompilerCache)
    interaction_mode: InteractionMode = InteractionMode.AUTO
    task_state: TaskState = field(default_factory=TaskState)
    compaction_summary: str = ""
    pending_summary: str | None = None
    pending_guidance: list[GuidanceEntry] = field(default_factory=list)
    session_date: str = ""
    runtime_guards: RuntimeGuardState = field(default_factory=RuntimeGuardState)
    turn_context: TurnExecutionContext | None = None
    runtime_profile: RuntimeProfile | None = None
    tool_policy: ToolPolicy | None = None
    tool_registry: Any | None = None
    workspace: str = ""
    host_id: int | None = None


_CURRENT_THREAD_EXECUTION_STATE: ContextVar[ThreadExecutionState | None] = ContextVar(
    "voidx_thread_execution_state",
    default=None,
)


def current_thread_execution_state() -> ThreadExecutionState | None:
    return _CURRENT_THREAD_EXECUTION_STATE.get()



def tool_registry_for(host: Any) -> Any:
    state = current_thread_execution_state()
    registry = getattr(state, "tool_registry", None) if state is not None else None
    return registry if registry is not None else host.tools

@dataclass
class _HostExecutionSnapshot:
    session: SessionInfo | None
    session_msg_cache: list[BaseMessage] | None
    context_cache: ContextCompilerCache
    interaction_mode: InteractionMode
    task_state: TaskState
    compaction_summary: str
    pending_summary: str | None
    session_date: str
    runtime_guards: RuntimeGuardState


def _snapshot_host(host: Any) -> _HostExecutionSnapshot:
    return _HostExecutionSnapshot(
        session=getattr(host, "_session", None),
        session_msg_cache=getattr(host, "_session_msg_cache", None),
        context_cache=getattr(host, "_context_cache", ContextCompilerCache()),
        interaction_mode=getattr(host, "_interaction_mode", InteractionMode.AUTO),
        task_state=getattr(host, "_task_state", TaskState()),
        compaction_summary=getattr(host, "_compaction_summary", ""),
        pending_summary=getattr(host, "_pending_summary", None),
        session_date=getattr(host, "_session_date", ""),
        runtime_guards=getattr(host, "_runtime_guards", RuntimeGuardState()),
    )


def _state_from_host(host: Any) -> ThreadExecutionState:
    return ThreadExecutionState(
        session=getattr(host, "_session", None),
        session_msg_cache=getattr(host, "_session_msg_cache", None),
        context_cache=getattr(host, "_context_cache", ContextCompilerCache()),
        interaction_mode=getattr(host, "_interaction_mode", InteractionMode.AUTO),
        task_state=getattr(host, "_task_state", TaskState()),
        compaction_summary=getattr(host, "_compaction_summary", ""),
        pending_summary=getattr(host, "_pending_summary", None),
        session_date=getattr(host, "_session_date", ""),
        runtime_guards=getattr(host, "_runtime_guards", RuntimeGuardState()),
        host_id=id(host),
    )


def _apply_state(host: Any, state: ThreadExecutionState | _HostExecutionSnapshot) -> None:
    host._session = state.session
    host._session_msg_cache = state.session_msg_cache
    host._context_cache = state.context_cache
    host._interaction_mode = state.interaction_mode
    host._task_state = state.task_state
    host._compaction_summary = state.compaction_summary
    host._pending_summary = state.pending_summary
    host._session_date = state.session_date
    host._runtime_guards = state.runtime_guards


def _state_key(
    session_id: str,
    thread_id: str,
    fallback_session: SessionInfo | None,
) -> str:
    resolved_session_id = session_id or (
        fallback_session.id if fallback_session is not None else ""
    )
    if not thread_id:
        return resolved_session_id
    return f"{resolved_session_id}\x1f{thread_id}"


def thread_execution_states(host: Any) -> dict[str, ThreadExecutionState]:
    states = getattr(host, "_thread_execution_states", None)
    if states is None:
        states = {}
        host._thread_execution_states = states
    return states


def clear_thread_execution_states(host: Any, session_id: str) -> None:
    """Drop all cached execution states belonging to one session."""
    if not session_id:
        return
    states = getattr(host, "_thread_execution_states", None)
    if not states:
        return
    prefix = f"{session_id}\x1f"
    for key in list(states):
        if key == session_id or key.startswith(prefix):
            del states[key]


async def _state_for_context(
    host: Any,
    session_id: str,
    *,
    thread_id: str = "",
    detached: bool = False,
) -> ThreadExecutionState:
    if detached:
        return ThreadExecutionState(
            session=None,
            session_msg_cache=None,
            context_cache=ContextCompilerCache(),
            host_id=id(host),
        )
    states = thread_execution_states(host)
    current_session = getattr(host, "_session", None)
    key = _state_key(session_id, thread_id, current_session)
    if key and key in states:
        return states[key]
    if session_id and current_session is not None and current_session.id == session_id:
        state = _state_from_host(host)
        states[key] = state
        return state
    if not session_id:
        state = _state_from_host(host)
        if key:
            states[key] = state
        return state

    target_session = await get_session(session_id)
    state = ThreadExecutionState(
        session=target_session,
        session_msg_cache=None,
        context_cache=ContextCompilerCache(),
        session_date=session_date(target_session),
        host_id=id(host),
    )
    if key:
        states[key] = state
    return state


async def _restore_state_runtime(host: Any, state: ThreadExecutionState) -> None:
    if state.session is None:
        return
    snapshot = await load_runtime_state(state.session.id)
    state.interaction_mode = snapshot.interaction_mode
    state.task_state = snapshot.task_state
    state.compaction_summary = snapshot.compaction_summary
    state.pending_summary = None
    if snapshot.session_time:
        state.session_date = snapshot.session_time


def _workspace_for_turn(host: Any, state: ThreadExecutionState, turn_context: TurnExecutionContext | None) -> str:
    if turn_context is not None and turn_context.workspace:
        return turn_context.workspace
    if state.session is not None:
        session_workspace = state.session.workspace or state.session.directory
        if session_workspace:
            return session_workspace
    return str(getattr(host, "_workspace", "") or "")


@asynccontextmanager
async def bind_thread_execution_context(
    host: Any,
    *,
    session_id: str = "",
    thread_id: str = "",
    turn_context: TurnExecutionContext | None = None,
) -> AsyncIterator[ThreadExecutionState]:
    """Bind host mutable state to one session for the duration of a turn."""

    detached = turn_context is not None and getattr(turn_context, "detached", False)
    state = await _state_for_context(
        host,
        session_id,
        thread_id=thread_id,
        detached=detached,
    )
    if session_id and not (
        getattr(host, "_session", None) is not None
        and getattr(host._session, "id", "") == session_id
        and (
            getattr(state.task_state, "current_goal", None) is not None
            or bool(getattr(state, "compaction_summary", ""))
        )
    ):
        await _restore_state_runtime(host, state)
    state.host_id = id(host)
    state.thread_id = thread_id or session_id
    workspace = _workspace_for_turn(host, state, turn_context)
    if turn_context is not None and workspace != turn_context.workspace:
        turn_context = turn_context.model_copy(update={"workspace": workspace})
    state.turn_context = turn_context
    state.runtime_profile = turn_context.runtime_profile if turn_context else None
    state.tool_policy = turn_context.tool_policy if turn_context else None
    profile_tool_registry_factory = getattr(
        host, "_profile_tool_registry_factory", None
    )

    tools = getattr(host, "tools", None)
    state.tool_registry = (
        profile_tool_registry_factory(
            tools,
            state.tool_policy,
            skills_api_provider=getattr(host, "skills_api_provider", None),
        )
        if tools is not None and callable(profile_tool_registry_factory)
        else None
    )
    state.workspace = workspace
    state.runtime_guards = RuntimeGuardState()
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    identity = ExecutionIdentity(
        thread_id=state.thread_id,
        session_id=state.session.id if state.session is not None else session_id,
    )
    try:
        with bind_execution_identity(identity):
            yield state
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)
        # Only sync state back to the host when the turn belongs to the host's
        # own session. A borrowed turn for a different session must not mutate
        # the host's session, task_state, compaction_summary, etc.
        orig_session = getattr(host, "_default_session", None)
        if orig_session is None or (state.session is not None and orig_session.id == state.session.id):
            _apply_state(host, state)
