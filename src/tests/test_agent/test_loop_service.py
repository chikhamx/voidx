from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.application.loop_service import LoopService
from voidx.agent.domain.loop import LoopSpec
from voidx.memory.thread_store import ThreadStore


@dataclass
class FakeScheduler:
    calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    async def run_prompt(self, prompt: str, *, display_text: str | None, session_id: str | None, **_kwargs):
        self.calls.append((prompt, display_text, session_id))
        return None


@pytest.mark.asyncio
async def test_loop_service_start_creates_repository_backed_status(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    status = await service.start("parent-1", LoopSpec(prompt="check build", interval_seconds=300))

    assert status.active is True
    assert status.parent_thread_id == "parent-1"
    assert status.loop_thread_id == "loop:parent-1:active"
    assert status.mode == "fixed"
    assert status.interval_seconds == 300
    assert status.iteration == 0
    assert status.last_summary == ""
    assert scheduler.calls == [("check build", "[loop] check build", "parent-1")]

    loaded = await store.load("loop:parent-1:active")
    assert loaded is not None
    assert loaded.thread.parent_thread_id == "parent-1"
    assert loaded.thread.session_id == "parent-1"


@pytest.mark.asyncio
async def test_loop_service_status_and_stop_are_repository_backed(tmp_path) -> None:
    service = LoopService(store=ThreadStore(), scheduler=FakeScheduler(), workspace=str(tmp_path))
    await service.start("parent-1", LoopSpec(prompt="check deploy"))

    status = await service.status("parent-1")
    assert status is not None
    assert status.mode == "dynamic"
    assert status.prompt_summary == "check deploy"

    stopped = await service.stop("parent-1")
    assert stopped is True
    assert await service.status("parent-1") is None


@pytest.mark.asyncio
async def test_loop_service_replaces_active_loop_for_same_parent(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent-1", LoopSpec(prompt="first"))
    status = await service.start("parent-1", LoopSpec(prompt="second", interval_seconds=60))

    assert status.prompt_summary == "second"
    assert status.mode == "fixed"
    assert [call[0] for call in scheduler.calls] == ["first", "second"]
