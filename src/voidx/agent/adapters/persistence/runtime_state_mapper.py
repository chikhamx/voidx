"""Mapping between Agent domain runtime and persisted memory snapshots."""

from __future__ import annotations

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.adapters.persistence.runtime_state_repository import RuntimeStateSnapshot


def agent_runtime_from_snapshot(snapshot: RuntimeStateSnapshot) -> SessionRuntimeState:
    return SessionRuntimeState(
        interaction_mode=snapshot.interaction_mode,
        task_state=snapshot.task_state.model_copy(deep=True),
        compaction_summary=snapshot.compaction_summary,
        session_time=snapshot.session_time,
    )


def snapshot_from_agent_runtime(runtime: SessionRuntimeState) -> RuntimeStateSnapshot:
    return RuntimeStateSnapshot(
        interaction_mode=runtime.interaction_mode,
        task_state=runtime.task_state.model_copy(deep=True),
        compaction_summary=runtime.compaction_summary,
        session_time=runtime.session_time,
    )
