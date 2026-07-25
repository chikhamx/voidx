"""Per-thread execution state isolation for graph turns."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.tool_policy import ToolPolicy
from voidx.agent.domain.turn_context import TurnExecutionContext

from voidx.agent.infrastructure.langgraph.runtime.runtime_guards import RuntimeGuardState
from voidx.agent.infrastructure.langgraph.runtime.topology import session_date
from voidx.agent.runtime_context import ContextCompilerCache, InteractionMode
from voidx.runtime.task_state import TaskState
from voidx.memory.service import SessionInfo, get_session, load_runtime_state
from voidx.runtime.execution_context import ExecutionIdentity, bind_execution_identity

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage



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
    session_date: str = ""
    runtime_guards: RuntimeGuardState = field(default_factory=RuntimeGuardState)
    turn_context: TurnExecutionContext | None = None
    runtime_profile: RuntimeProfile | None = None
    tool_policy: ToolPolicy | None = None
    workspace: str = ""


_CURRENT_THREAD_EXECUTION_STATE: ContextVar[ThreadExecutionState | None] = ContextVar(
    "voidx_thread_execution_state",
    default=None,
)


def current_thread_execution_state() -> ThreadExecutionState | None:
    return _CURRENT_THREAD_EXECUTION_STATE.get()


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


def _state_key(session_id: str, fallback_session: SessionInfo | None) -> str:
    return session_id or (fallback_session.id if fallback_session is not None else "")


def thread_execution_states(host: Any) -> dict[str, ThreadExecutionState]:
    states = getattr(host, "_thread_execution_states", None)
    if states is None:
        states = {}
        host._thread_execution_states = states
    return states


async def _state_for_context(host: Any, session_id: str) -> ThreadExecutionState:
    states = thread_execution_states(host)
    current_session = getattr(host, "_session", None)
    key = _state_key(session_id, current_session)
    if key and key in states:
        return states[key]
    if session_id and current_session is not None and current_session.id == session_id:
        state = _state_from_host(host)
        states[key] = state
        return state

    if session_id:
        target_session = await get_session(session_id)
        state = ThreadExecutionState(
            session=target_session,
            session_msg_cache=None,
            context_cache=ContextCompilerCache(),
            session_date=session_date(target_session),
        )
        if key:
            states[key] = state
        return state

    state = _state_from_host(host)
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


@asynccontextmanager
async def bind_thread_execution_context(
    host: Any,
    *,
    session_id: str = "",
    thread_id: str = "",
    turn_context: TurnExecutionContext | None = None,
) -> AsyncIterator[ThreadExecutionState]:
    """Bind host mutable state to one session for the duration of a turn."""

    state = await _state_for_context(host, session_id)
    if session_id and not (
        getattr(host, "_session", None) is not None
        and getattr(host._session, "id", "") == session_id
        and (
            getattr(state.task_state, "current_goal", None) is not None
            or bool(getattr(state, "compaction_summary", ""))
        )
    ):
        await _restore_state_runtime(host, state)
    state.thread_id = thread_id or session_id
    state.turn_context = turn_context
    state.runtime_profile = turn_context.runtime_profile if turn_context else None
    state.tool_policy = turn_context.tool_policy if turn_context else None
    state.workspace = turn_context.workspace if turn_context else ""
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
        orig_session = getattr(host, "_session", None)
        if orig_session is None or (state.session is not None and orig_session.id == state.session.id):
            _apply_state(host, state)
