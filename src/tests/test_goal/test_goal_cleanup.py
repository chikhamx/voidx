from __future__ import annotations

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.domain.automation.goal import (
    GOAL_PROFILE,
    GoalDecision,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
    WorkCheckpoint,
)
from voidx.agent.domain.guidance import Guidance
from voidx.agent.domain.thread import AgentThreadState, LifecycleState
from voidx.persistence.jsonl import session_dir
from voidx.agent.ports.persistence import GoalProtocolConflict
from tests.goal_protocol_helpers import submit_fenced_goal_protocol


def _profile_snapshot() -> AgentProfileSnapshot:
    return AgentProfileSnapshot(
        profile_id="goal",
        revision=1,
        source="bundled",
        content_hash="content",
        snapshot_hash="snapshot",
        canonical_payload={"profile_id": "goal"},
    )


def _boundary_kwargs() -> dict:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen-cleanup",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="main-cleanup",
        parent_thread_id="main-thread",
        workspace="/workspace",
        profile_snapshot={"profile_id": "goal"},
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-cleanup",
        work_session_id="work-cleanup",
        evaluator_session_id="eval-cleanup",
    )
    return {
        "generation": spec.generation,
        "main_session_id": "main-cleanup",
        "evaluator_session_id": "eval-cleanup",
        "work_session_id": "work-cleanup",
        "goal_thread_id": "goal:main-cleanup:gen-cleanup",
        "parent_thread_id": "main-thread",
        "workspace": "/workspace",
        "profile_id": "goal",
        "profile_snapshot": _profile_snapshot(),
        "thread_profile": GOAL_PROFILE,
        "thread_state": AgentThreadState(
            thread_id="goal:main-cleanup:gen-cleanup",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        "protocol": GoalProtocolRecord.submitted(
            protocol_id="protocol-init-cleanup",
            parent_session_id="main-cleanup",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-cleanup",
            session_id="main-cleanup",
            payload=snapshot,
        ),
    }


async def _finish_goal(store: ThreadStore) -> None:
    await store.ensure_session(
        "main-cleanup", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())

    checkpoint = WorkCheckpoint(
        generation="gen-cleanup",
        attempt_number=1,
        summary="implementation complete",
        progress="meaningful",
        work_turn_id="turn-work-cleanup",
    )
    checkpoint_record = GoalProtocolRecord.submitted(
        protocol_id="protocol-checkpoint-cleanup",
        parent_session_id="main-cleanup",
        generation="gen-cleanup",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn-work-cleanup",
        session_id="work-cleanup",
        payload=checkpoint,
    )
    await submit_fenced_goal_protocol(store, checkpoint_record)
    await store.project_goal_protocol(checkpoint_record.protocol_id)

    decision = GoalDecision(
        generation="gen-cleanup",
        attempt_number=1,
        status="finished",
        summary="accepted",
        reason="acceptance condition verified",
        progress="meaningful",
    )
    decision_record = GoalProtocolRecord.submitted(
        protocol_id="protocol-decision-cleanup",
        parent_session_id="main-cleanup",
        generation="gen-cleanup",
        phase="decision",
        attempt_number=1,
        turn_id="turn-evaluator-cleanup",
        session_id="eval-cleanup",
        payload=decision,
    )
    await submit_fenced_goal_protocol(store, decision_record)
    await store.project_goal_protocol(decision_record.protocol_id)


@pytest.mark.asyncio
async def test_archive_goal_generation_requires_terminal_and_is_idempotent(
    tmp_path,
) -> None:
    store = ThreadStore(tmp_path / "cleanup-archive.db")
    await store.ensure_session(
        "main-cleanup", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())

    with pytest.raises(GoalProtocolConflict, match="terminal"):
        await store.archive_goal_generation("gen-cleanup")

    await _finish_goal(store)
    archived = await store.archive_goal_generation("gen-cleanup")
    repeated = await store.archive_goal_generation("gen-cleanup")

    assert archived.archived_at is not None
    assert repeated == archived
    assert await store.get_session("main-cleanup") is not None
    assert await store.get_session("work-cleanup") is not None
    assert await store.get_session("eval-cleanup") is not None
    assert len(await store.list_goal_protocols("gen-cleanup")) == 3


@pytest.mark.asyncio
async def test_archived_goal_rejects_new_guidance_and_phase_wakeup(tmp_path) -> None:
    store = ThreadStore(tmp_path / "cleanup-archived-write.db")
    await _finish_goal(store)
    binding = await store.get_goal_generation("gen-cleanup")
    assert binding is not None
    archived = await store.archive_goal_generation("gen-cleanup")
    assert archived.archived_at is not None

    with pytest.raises(GoalProtocolConflict, match="archived"):
        await store.submit_guidance(
            Guidance(
                guidance_id="archived-guidance",
                text="do not reopen this generation",
                target_thread_id=binding.goal_thread_id,
                target_run_id="gen-cleanup",
                target_phase="any",
            )
        )

    with pytest.raises(GoalProtocolConflict, match="archived"):
        await store.ensure_goal_phase_outbox("gen-cleanup")


    with pytest.raises(GoalProtocolConflict, match="archived"):
        await store.acquire_goal_generation_lease(
            "gen-cleanup", "archived-recovery", lease_seconds=60
        )

    after_archive = GoalProtocolRecord.submitted(
        protocol_id="protocol-after-archive",
        parent_session_id="main-cleanup",
        generation="gen-cleanup",
        phase="decision",
        attempt_number=2,
        turn_id="turn-after-archive",
        session_id="eval-cleanup",
        payload=GoalDecision(
            generation="gen-cleanup",
            attempt_number=2,
            status="finished",
            summary="must not be accepted",
            reason="archived",
            progress="none",
        ),
    )
    with pytest.raises(GoalProtocolConflict, match="archived"):
        await store.submit_goal_protocol(after_archive)


@pytest.mark.asyncio
async def test_cleanup_goal_generation_removes_internal_transcript_directories(tmp_path) -> None:
    store = ThreadStore(tmp_path / "cleanup-directories.db")
    await _finish_goal(store)
    binding = await store.get_goal_generation("gen-cleanup")
    assert binding is not None

    for session_id in (binding.work_session_id, binding.evaluator_session_id):
        directory = session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "messages.jsonl").write_text("{\"role\":\"assistant\"}\n")
        assert directory.exists()

    await store.cleanup_goal_generation("gen-cleanup")

    assert not session_dir(binding.work_session_id).exists()
    assert not session_dir(binding.evaluator_session_id).exists()




@pytest.mark.asyncio
async def test_cleanup_goal_generation_preserves_audit_and_is_idempotent(tmp_path) -> None:
    store = ThreadStore(tmp_path / "cleanup-runtime.db")
    await _finish_goal(store)
    binding = await store.get_goal_generation("gen-cleanup")
    assert binding is not None
    await store.submit_guidance(
        Guidance(
            guidance_id="cleanup-guidance",
            text="keep the evidence narrow",
            target_thread_id=binding.goal_thread_id,
            target_run_id="gen-cleanup",
            target_phase="any",
        )
    )

    cleaned = await store.cleanup_goal_generation("gen-cleanup")
    repeated = await store.cleanup_goal_generation("gen-cleanup")

    assert cleaned.archived_at is not None
    assert repeated == cleaned
    assert await store.load(binding.goal_thread_id) is None
    assert await store.get_guidance("cleanup-guidance") is None
    assert await store.get_session("main-cleanup") is not None
    assert await store.get_session("work-cleanup") is None
    assert await store.get_session("eval-cleanup") is None
    assert await store.get_goal_generation("gen-cleanup") is None
    assert await store.list_goal_protocols("gen-cleanup") == []
    assert await store.list_pending_outbox(binding.goal_thread_id) == []
    assert store._conn is not None
    tombstone = store._conn.execute(
        "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
        ("gen-cleanup",),
    ).fetchone()
    assert tombstone is not None
    assert tombstone["cleanup_epoch"] == 1
    assert tombstone["main_session_id"] == "main-cleanup"
    assert tombstone["work_session_id"] == "work-cleanup"
    assert tombstone["evaluator_session_id"] == "eval-cleanup"
    assert tombstone["status"] == "committed"
    assert tombstone["requested_at"]
    assert tombstone["completed_at"]
    assert tombstone["last_error"] == ""
    assert store._conn.execute(
        "SELECT COUNT(*) FROM runtime_turn_attempts WHERE thread_id = ?",
        (binding.goal_thread_id,),
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM runtime_outbox WHERE thread_id = ?",
        (binding.goal_thread_id,),
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_pending_cleanup_tombstone_rejects_all_goal_writes(tmp_path) -> None:
    store = ThreadStore(tmp_path / "cleanup-pending-writes.db")
    await _finish_goal(store)
    binding = await store.get_goal_generation("gen-cleanup")
    assert binding is not None
    assert store._conn is not None
    store._conn.execute(
        """INSERT INTO goal_generation_cleanup (
               generation, cleanup_epoch, main_session_id,
               work_session_id, evaluator_session_id, status,
               requested_at, completed_at, last_error
           ) VALUES (?, 1, ?, ?, ?, 'pending', ?, NULL, '')""",
        (
            "gen-cleanup",
            binding.main_session_id,
            binding.work_session_id,
            binding.evaluator_session_id,
            "2026-08-25T00:00:00+00:00",
        ),
    )

    store._conn.commit()

    with pytest.raises(GoalProtocolConflict, match="cleanup"):
        await store.submit_guidance(
            Guidance(
                guidance_id="pending-cleanup-guidance",
                text="must not be accepted",
                target_thread_id=binding.goal_thread_id,
                target_run_id="gen-cleanup",
                target_phase="any",
            )
        )
    with pytest.raises(GoalProtocolConflict, match="cleanup"):
        await store.ensure_goal_phase_outbox("gen-cleanup")
    with pytest.raises(GoalProtocolConflict, match="cleanup"):
        await store.acquire_goal_generation_lease(
            "gen-cleanup", "pending-cleanup-recovery", lease_seconds=60
        )

    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-pending-cleanup",
        parent_session_id=binding.main_session_id,
        generation="gen-cleanup",
        phase="decision",
        attempt_number=2,
        turn_id="turn-pending-cleanup",
        session_id=binding.evaluator_session_id,
        payload=GoalDecision(
            generation="gen-cleanup",
            attempt_number=2,
            status="finished",
            summary="must not be accepted",
            reason="cleanup pending",
            progress="none",
        ),
    )
    with pytest.raises(GoalProtocolConflict, match="cleanup"):
        await store.submit_goal_protocol(record)


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_pending_tombstone_and_retries(
    tmp_path, monkeypatch
) -> None:
    import voidx.agent.adapters.persistence.thread_repository as repository

    store = ThreadStore(tmp_path / "cleanup-retry.db")
    await _finish_goal(store)
    binding = await store.get_goal_generation("gen-cleanup")
    assert binding is not None

    async def fail_once(session_ids):
        raise OSError("transcript deletion failed")

    monkeypatch.setattr(repository, "delete_session_directories", fail_once)
    with pytest.raises(OSError, match="transcript deletion failed"):
        await store.cleanup_goal_generation("gen-cleanup")

    assert store._conn is not None
    pending = store._conn.execute(
        "SELECT * FROM goal_generation_cleanup WHERE generation = ?",
        ("gen-cleanup",),
    ).fetchone()
    assert pending is not None
    assert pending["status"] == "pending"
    assert pending["cleanup_epoch"] == 1
    assert pending["last_error"] == "transcript deletion failed"
    assert await store.get_goal_generation("gen-cleanup") is not None

    async def succeed(session_ids):
        return None

    monkeypatch.setattr(repository, "delete_session_directories", succeed)
    cleaned = await store.cleanup_goal_generation("gen-cleanup")
    assert cleaned.archived_at is not None
    committed = store._conn.execute(
        "SELECT status, cleanup_epoch, last_error FROM goal_generation_cleanup "
        "WHERE generation = ?",
        ("gen-cleanup",),
    ).fetchone()
    assert tuple(committed) == ("committed", 1, "")
    assert await store.get_session("work-cleanup") is None
    assert await store.get_session("eval-cleanup") is None
    assert await store.list_pending_outbox(binding.goal_thread_id) == []
    assert store._conn is not None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM runtime_turn_attempts WHERE thread_id = ?",
        (binding.goal_thread_id,),
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM runtime_outbox WHERE thread_id = ?",
        (binding.goal_thread_id,),
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cleanup_goal_generation_rejects_non_terminal_generation(tmp_path) -> None:
    store = ThreadStore(tmp_path / "cleanup-active.db")
    await store.ensure_session(
        "main-cleanup", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())

    with pytest.raises(GoalProtocolConflict, match="terminal"):
        await store.cleanup_goal_generation("gen-cleanup")


@pytest.mark.asyncio
async def test_main_session_delete_cancels_and_cleans_all_goal_bundles(tmp_path) -> None:
    from voidx.agent.application.automation.goal.cleanup import GoalCleanupCoordinator

    store = ThreadStore(tmp_path / "cleanup-main.db")
    await store.ensure_session(
        "main-cleanup", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())
    binding = await store.get_goal_generation("gen-cleanup")
    assert binding is not None

    async def delete_main(session_id: str) -> None:
        assert await store.get_goal_generation("gen-cleanup") is None
        await store._write(
            lambda conn: conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        )

    cleaned = await GoalCleanupCoordinator(
        store=store,
        delete_main_session=delete_main,
    ).delete_main_session("main-cleanup")

    assert cleaned == ["gen-cleanup"]
    assert await store.get_session("main-cleanup") is None
    assert await store.get_session(binding.work_session_id) is None
    assert await store.get_session(binding.evaluator_session_id) is None
    assert await store.list_pending_outbox(binding.goal_thread_id) == []
    assert store._conn is not None
    tombstone = store._conn.execute(
        "SELECT status FROM goal_generation_cleanup WHERE generation = ?",
        ("gen-cleanup",),
    ).fetchone()
    assert tombstone["status"] == "committed"


@pytest.mark.asyncio
async def test_goal_cleanup_reconciler_retries_pending_tombstone(
    tmp_path, monkeypatch
) -> None:
    import voidx.agent.adapters.persistence.thread_repository as repository
    from voidx.agent.application.automation.goal.cleanup import GoalCleanupCoordinator

    store = ThreadStore(tmp_path / "cleanup-reconcile-pending.db")
    await _finish_goal(store)

    async def fail_delete(session_ids):
        raise OSError("cleanup interrupted")

    monkeypatch.setattr(repository, "delete_session_directories", fail_delete)
    with pytest.raises(OSError, match="cleanup interrupted"):
        await store.cleanup_goal_generation("gen-cleanup")

    monkeypatch.setattr(
        repository,
        "delete_session_directories",
        __import__("voidx.persistence.jsonl", fromlist=["delete_session_directories"]).delete_session_directories,
    )
    reconciled = await GoalCleanupCoordinator(store=store).reconcile_orphans()

    assert reconciled == ["gen-cleanup"]
    assert await store.get_goal_generation("gen-cleanup") is None
    assert store._conn is not None
    status = store._conn.execute(
        "SELECT status FROM goal_generation_cleanup WHERE generation = ?",
        ("gen-cleanup",),
    ).fetchone()
    assert status["status"] == "committed"


@pytest.mark.asyncio
async def test_goal_cleanup_reconciler_removes_committed_resurrected_directories(
    tmp_path,
) -> None:
    from voidx.agent.application.automation.goal.cleanup import GoalCleanupCoordinator

    store = ThreadStore(tmp_path / "cleanup-reconcile-committed.db")
    await _finish_goal(store)
    binding = await store.cleanup_goal_generation("gen-cleanup")
    for session_id in (binding.work_session_id, binding.evaluator_session_id):
        directory = session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "messages.jsonl").write_text("{}\n", encoding="utf-8")

    reconciled = await GoalCleanupCoordinator(store=store).reconcile_orphans()

    assert reconciled == ["gen-cleanup"]
    assert not session_dir(binding.work_session_id).exists()
    assert not session_dir(binding.evaluator_session_id).exists()


@pytest.mark.asyncio
async def test_failed_generation_cleanup_removes_failure_but_preserves_public_summary(
    tmp_path,
) -> None:
    from voidx.agent.domain.automation.goal import GoalRuntimeFailure

    store = ThreadStore(tmp_path / "cleanup-failed.db")
    await store.ensure_session(
        "main-cleanup", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())
    failure = GoalRuntimeFailure(
        generation="gen-cleanup",
        observed_sequence=0,
        reason="journal corruption",
        evidence=("sequence mismatch",),
    )
    await store.fail_goal_generation(failure)

    await store.cleanup_goal_generation("gen-cleanup")

    assert await store.get_goal_runtime_failure("gen-cleanup") is None
    assert await store.get_goal_generation("gen-cleanup") is None
    summaries = await store.list_goal_public_summaries("main-cleanup")
    assert len(summaries) == 1
    assert summaries[0]["generation"] == "gen-cleanup"
    assert store._conn is not None
    await store._write(
        lambda conn: conn.execute("DELETE FROM sessions WHERE id = ?", ("main-cleanup",))
    )
    assert await store.list_goal_public_summaries("main-cleanup") == []


@pytest.mark.asyncio
async def test_goal_cleanup_reconciler_discovers_orphan_without_tombstone(tmp_path) -> None:
    from voidx.agent.application.automation.goal.cleanup import GoalCleanupCoordinator

    store = ThreadStore(tmp_path / "cleanup-orphan.db")
    await store.ensure_session(
        "main-cleanup", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())
    assert store._conn is not None
    store._conn.execute("PRAGMA foreign_keys=OFF")
    store._conn.execute("DELETE FROM sessions WHERE id = ?", ("main-cleanup",))
    store._conn.commit()
    store._conn.execute("PRAGMA foreign_keys=ON")

    reconciled = await GoalCleanupCoordinator(store=store).reconcile_orphans()

    assert reconciled == ["gen-cleanup"]
    assert await store.get_goal_generation("gen-cleanup") is None
    tombstone = store._conn.execute(
        "SELECT status FROM goal_generation_cleanup WHERE generation = ?",
        ("gen-cleanup",),
    ).fetchone()
    assert tombstone["status"] == "committed"
