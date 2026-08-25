"""Goal generation bundle cleanup coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from voidx.agent.ports.persistence import ThreadStore


DeleteSession = Callable[[str], Awaitable[None]]


class GoalCleanupCoordinator:
    def __init__(
        self,
        *,
        store: ThreadStore,
        delete_main_session: DeleteSession | None = None,
    ) -> None:
        self._store = store
        self._delete_main_session = delete_main_session

    async def delete_main_session(self, main_session_id: str) -> list[str]:
        main_session_id = main_session_id.strip()
        if not main_session_id:
            raise ValueError("main session id must not be empty")

        bindings = await self._store.list_goal_generations(main_session_id)
        for binding in bindings:
            await self._store.prepare_goal_generation_cleanup(
                binding.generation,
                reason="Goal cancelled because its main session was deleted.",
            )

        tombstones = await self._store.list_goal_cleanup_tombstones()
        generations = [binding.generation for binding in bindings]
        generations.extend(
            row["generation"]
            for row in tombstones
            if row["main_session_id"] == main_session_id
            and row["generation"] not in generations
        )
        for generation in generations:
            await self._store.reconcile_goal_cleanup(generation)

        delete_main = self._delete_main_session
        if delete_main is None:
            raise ValueError("main session deleter is required")
        await delete_main(main_session_id)
        return generations

    async def reconcile_orphans(self) -> list[str]:
        await self._store.prepare_orphan_goal_generation_cleanups()
        tombstones = await self._store.list_goal_cleanup_tombstones()
        reconciled: list[str] = []
        for row in tombstones:
            await self._store.reconcile_goal_cleanup(row["generation"])
            reconciled.append(row["generation"])
        return reconciled


__all__ = ["GoalCleanupCoordinator"]
