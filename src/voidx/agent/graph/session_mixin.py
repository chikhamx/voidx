"""Runtime session state persistence for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.memory.runtime_state import (
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_runtime_state,
    save_runtime_state,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


class GraphSessionMixin:
    async def _restore_runtime_state(self: GraphRunLoopHost) -> None:
        if self._session is None:
            return
        snapshot = await load_runtime_state(self._session.id)
        self._interaction_mode = snapshot.interaction_mode
        self._task_state = snapshot.task_state
        self._task_run = snapshot.task_run
        self._compaction_summary = snapshot.compaction_summary

    async def _persist_runtime_state(self: GraphRunLoopHost) -> None:
        if self._session is None:
            return
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun, TaskState

        interaction_mode = getattr(self, "_interaction_mode", None) or InteractionMode.AUTO
        task_state = getattr(self, "_task_state", None) or TaskState()
        task_run = getattr(self, "_task_run", None) or TaskRun()
        await save_runtime_state(
            self._session.id,
            RuntimeStateSnapshot(
                interaction_mode=interaction_mode,
                task_state=task_state,
                task_run=task_run,
                compaction_summary=getattr(self, "_compaction_summary", ""),
            ),
        )

    async def _clear_runtime_state(self: GraphRunLoopHost) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun, TaskState

        if self._session is not None:
            await clear_runtime_state(self._session.id)
        self._interaction_mode = InteractionMode.AUTO
        self._task_state = TaskState()
        self._task_run = TaskRun()
        self._compaction_summary = ""
        self._pending_summary = None
