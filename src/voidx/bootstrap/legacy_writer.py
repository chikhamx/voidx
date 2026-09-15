"""Borrow the OS workspace owner for the lifetime of a legacy graph turn."""
from contextvars import ContextVar

from voidx.agent.adapters.persistence.headless_locks import OwnedWorkspaceWriteLock
from voidx.agent.ports.workspace_lock import DelegatingWorkspaceWriteLock


class LegacyTurnWriter(DelegatingWorkspaceWriteLock):
    def __init__(self, workspace):
        super().__init__()
        self.workspace = workspace
        self._lease = ContextVar('legacy_turn_writer', default=None)

    async def acquire_workspace_write_lock(self, thread_id):
        lease = self._lease.get()
        if lease is None:
            raise RuntimeError('Legacy write requires a live turn owner')
        await lease.acquire_workspace_write_lock(thread_id)
        return await super().acquire_workspace_write_lock(thread_id)

    def release_workspace_write_lock(self, thread_id):
        super().release_workspace_write_lock(thread_id)
        lease = self._lease.get()
        if lease is not None:
            lease.release_workspace_write_lock(thread_id)


class LegacyWriterTurnEngine:
    def __init__(self, engine, writer):
        self._engine = engine
        self._writer = writer

    @property
    def session_id(self):
        return self._engine.session_id

    @property
    def last_evidence(self):
        return self._engine.last_evidence

    async def run(self, *args, **kwargs):
        owner = OwnedWorkspaceWriteLock(self._writer.workspace)
        context = kwargs.get('context')
        token = self._writer._lease.set(owner.child(context.thread_id if context is not None else ''))
        try:
            return await self._engine.run(*args, **kwargs)
        finally:
            owner.close()
            self._writer._lease.reset(token)
