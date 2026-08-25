from __future__ import annotations

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.automation.goal import GoalProtocolRecord
from voidx.agent.domain.thread import RuntimeOutboxItem, ThreadAttempt


async def submit_fenced_goal_protocol(
    store: ThreadStore,
    record: GoalProtocolRecord,
    *,
    lease_owner: str = "goal-protocol-test",
) -> tuple[RuntimeOutboxItem, ThreadAttempt]:
    binding = await store.get_goal_generation(record.generation)
    assert binding is not None
    expected_phase = {"checkpoint": "work", "decision": "evaluator"}[record.phase]
    pending = await store.list_pending_outbox(binding.goal_thread_id or "")
    outbox = next(
        item
        for item in pending
        if item.kind == "goal_prompt"
        and item.payload.get("phase") == expected_phase
        and int(item.payload.get("attempt_number", -1)) == record.attempt_number
    )
    claimed = await store.claim_outbox(
        outbox.outbox_id,
        lease_owner=lease_owner,
        lease_seconds=60,
    )
    assert claimed is not None
    attempt = await store.begin_attempt(
        thread_id=outbox.thread_id,
        source_outbox_id=outbox.outbox_id,
        input_frame={"kind": outbox.kind, **outbox.payload},
        expected_state_version=outbox.expected_state_version,
        lease_owner=lease_owner,
        lease_seconds=60,
    )
    started = await store.mark_side_effect_started(
        attempt.attempt_id,
        lease_owner=lease_owner,
        fencing_token=attempt.fencing_token,
    )
    assert started is not None
    await store.submit_goal_protocol(
        record,
        attempt_id=started.attempt_id,
        lease_owner=lease_owner,
        fencing_token=started.fencing_token,
    )
    return outbox, started
