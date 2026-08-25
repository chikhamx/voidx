from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.application.guidance_service import GuidanceService
from voidx.agent.domain.guidance import Guidance


@dataclass
class RecordingStore:
    persisted: list[Guidance] = field(default_factory=list)
    callbacks_seen: list[int] = field(default_factory=list)

    def submit_guidance_sync(self, guidance: Guidance) -> Guidance:
        self.persisted.append(guidance)
        self.callbacks_seen.append(len(self.persisted))
        return guidance


@pytest.fixture
def store() -> RecordingStore:
    return RecordingStore()


def test_submit_persists_before_callback_and_freezes_universal_target(store: RecordingStore) -> None:
    callback_order: list[tuple[str, int]] = []
    service = GuidanceService(
        store,
        on_submitted=lambda guidance: callback_order.append(
            (guidance.guidance_id, len(store.persisted))
        ),
        id_factory=lambda: "guidance-fixed",
        max_chars=12,
    )

    submitted = service.submit_guidance(
        "  keep   the   API compatible  ",
        source="guard",
        thread_id="thread-1",
        session_id="session-1",
        run_id="run-1",
        phase="work",
    )

    assert submitted is not None
    assert submitted.guidance_id == "guidance-fixed"
    assert submitted.text == "keep the API"
    assert submitted.truncated is True
    assert submitted.source == "guard"
    assert submitted.target_thread_id == "thread-1"
    assert submitted.target_session_id == "session-1"
    assert submitted.target_run_id == "run-1"
    assert submitted.target_phase == "work"
    assert callback_order == [("guidance-fixed", 1)]


def test_empty_guidance_is_not_persisted_or_notified(store: RecordingStore) -> None:
    notified: list[Guidance] = []
    service = GuidanceService(store, on_submitted=notified.append)

    assert service.submit_guidance(" \n\t ") is None
    assert store.persisted == []
    assert notified == []


@pytest.mark.asyncio
async def test_delivery_lifecycle_delegates_to_the_same_store() -> None:
    calls: list[tuple[str, tuple, dict]] = []

    class Store:
        async def bind_guidance(self, *args, **kwargs):
            calls.append(("bind", args, kwargs))
            return ["bound"]

        async def release_guidance(self, *args, **kwargs):
            calls.append(("release", args, kwargs))

        async def consume_guidance(self, *args, **kwargs):
            calls.append(("consume", args, kwargs))

    service = GuidanceService(Store())

    assert await service.bind_delivery(
        "delivery-1", session_id="session-1", run_id="run-1", phase="evaluator"
    ) == ["bound"]
    await service.release_delivery("delivery-1")
    await service.commit_delivery("delivery-1")

    assert calls == [
        (
            "bind",
            ("delivery-1",),
            {"session_id": "session-1", "run_id": "run-1", "phase": "evaluator"},
        ),
        ("release", ("delivery-1",), {}),
        ("consume", ("delivery-1",), {}),
    ]
