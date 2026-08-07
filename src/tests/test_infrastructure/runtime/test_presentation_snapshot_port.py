"""Presentation transcript snapshot port integration tests."""

from types import SimpleNamespace

import pytest

from voidx.agent.infrastructure.langgraph.runtime.session_runtime import SessionRuntime


class RecordingSnapshotPort:
    def __init__(self, *, restored: bool = True) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.restored = restored

    async def persist_current(self, session_id: str) -> None:
        self.calls.append(("persist", session_id))

    async def restore_current(self, session_id: str, *, append: bool = False) -> bool:
        self.calls.append(("restore", session_id, append))
        return self.restored

    async def clear(self, session_id: str) -> None:
        self.calls.append(("clear", session_id))


@pytest.mark.asyncio
async def test_session_runtime_delegates_transcript_snapshot_to_port():
    snapshots = RecordingSnapshotPort(restored=True)
    runtime = SessionRuntime(
        SimpleNamespace(_session=SimpleNamespace(id="session-1")),
        presentation_snapshots=snapshots,
    )

    await runtime.persist_transcript_snapshot()
    restored = await runtime.restore_transcript_snapshot(append=True)

    assert restored is True
    assert snapshots.calls == [
        ("persist", "session-1"),
        ("restore", "session-1", True),
    ]


@pytest.mark.asyncio
async def test_session_runtime_without_session_does_not_call_snapshot_port():
    snapshots = RecordingSnapshotPort()
    runtime = SessionRuntime(
        SimpleNamespace(_session=None),
        presentation_snapshots=snapshots,
    )

    await runtime.persist_transcript_snapshot()
    restored = await runtime.restore_transcript_snapshot()

    assert restored is False
    assert snapshots.calls == []
