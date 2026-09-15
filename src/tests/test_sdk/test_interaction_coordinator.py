"""Run-owned interaction decisions and bounded publication."""
import asyncio

import pytest

from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
from voidx.agent.ports.events import NullSemanticEventPublisher
from voidx.tooling.domain.interaction import InteractionRequest, InteractionResponse, UserResponse

IDENTITY = dict(session_id="s", thread_id="t", turn_id="r")


def request(id="i", **kw):
    return InteractionRequest(**(dict(**IDENTITY, interaction_id=id, input_kind="choice",
        purpose="generic", prompt="Choose", choices=[dict(label="Yes", value="yes")]) | kw))


def response(**kw):
    return InteractionResponse(**(dict(**IDENTITY, value="yes") | kw))


class Publisher:
    def __init__(self, block=None):
        self.events = []
        self.block = block
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, event):
        if event.kind == self.block:
            self.entered.set()
            await self.release.wait()
        self.events.append(event)


async def ready():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def coordinator(publisher, **kw):
    return InteractionCoordinator(publisher, **IDENTITY, **kw)


def test_answer_validation_ownership_duplicates_and_id_reuse():
    async def run():
        p = Publisher()
        c = coordinator(p)
        t = asyncio.create_task(c.request(request()))
        await ready()
        for bad in [response(value="bad"), response(free_text=True), response(session_id="other"), response(scope="always")]:
            with pytest.raises(ValueError):
                await c.submit_interaction("i", bad)
        assert not await c.submit_interaction("unknown", response())
        assert await c.submit_interaction("i", response())
        assert not await c.submit_interaction("i", response())
        assert (await t).value == "yes"
        assert [e.kind for e in p.events] == ["interaction.required", "interaction.resolved"]
        with pytest.raises(ValueError):
            await c.request(request())
        with pytest.raises(ValueError):
            await c.request(request("other", turn_id="wrong"))
        assert await c.cleanup("completed") == ()
        assert UserResponse(value="yes").value == "yes"
    asyncio.run(run())


@pytest.mark.parametrize("kwargs,answer", [
    ({"allow_free_text": True}, {"value": "custom", "free_text": True}),
    ({"input_kind": "text"}, {"value": "words", "free_text": True}),
    ({"input_kind": "permission", "purpose": "permission", "allowed_scopes": ["once"]}, {"scope": "once"}),
])
def test_free_text_and_scopes(kwargs, answer):
    async def run():
        c = coordinator(NullSemanticEventPublisher())
        t = asyncio.create_task(c.request(request(**kwargs)))
        await ready()
        assert await c.submit_interaction("i", response(**answer))
        assert (await t).value == answer.get("value", "yes")
        assert await c.cleanup("completed") == ()
    asyncio.run(run())


@pytest.mark.parametrize("purpose,decision", [("permission", "deny"), ("checkpoint", "rejected"), ("clarify", "skipped"), ("goal", "rejected"), ("loop", "rejected"), ("generic", "cancelled")])
@pytest.mark.parametrize("reason", ["timed_out", "dismissed", "user_rejected", "task_cancelled"])
def test_policy(purpose, decision, reason):
    async def run():
        c = coordinator(NullSemanticEventPublisher())
        t = asyncio.create_task(c.request(request(purpose=purpose, timeout=.01)))
        await ready()
        if reason == "dismissed":
            await c.submit_interaction("i", response(cancelled=True))
        elif reason == "user_rejected":
            await c.submit_interaction("i", response(rejected=True))
            if purpose == "generic":
                expected = "rejected"
            else:
                expected = decision
        elif reason == "task_cancelled":
            c.begin_cancel()
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
            tail = await c.cleanup("cancelled")
            assert tail[0].payload.resolution.resolution_reason == reason
            assert tail[0].payload.resolution.decision != "auto_approved"
            return
        result = await t
        assert result.resolution_reason == reason
        assert result.decision == (expected if reason == "user_rejected" else decision)
        assert not await c.submit_interaction("i", response())
    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["goal", "loop"])
def test_legacy_policy_never_approves_task_cancellation(purpose):
    async def run():
        c = coordinator(NullSemanticEventPublisher(), legacy_auto_approve=True)
        assert (await c.request(request(purpose=purpose, timeout=.001))).decision == "auto_approved"
        t = asyncio.create_task(c.request(request("second", purpose=purpose)))
        await ready()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        tail = await c.cleanup("cancelled")
        assert tail[0].payload.resolution.decision == "rejected"
    asyncio.run(run())


@pytest.mark.parametrize("block", ["interaction.required", "interaction.resolved"])
def test_blocked_publication_cancellation_and_no_leaked_tasks(block):
    async def run():
        before = asyncio.all_tasks()
        p = Publisher(block)
        c = coordinator(p)
        t = asyncio.create_task(c.request(request(timeout=.01)))
        await ready()
        if block.endswith("resolved"):
            assert await asyncio.wait_for(c.submit_interaction("i", response()), .1)
        await p.entered.wait()
        await asyncio.sleep(.02)
        assert not t.done()
        c.begin_cancel()
        assert not await c.submit_interaction("i", response())
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        tail = await c.cleanup("cancelled")
        if block.endswith("required"):
            assert tail == ()
        else:
            assert len(tail) == 1
            assert tail[0].payload.resolution.resolution_reason == "answered"
        assert await c.cleanup("cancelled") == ()
        assert asyncio.all_tasks() == before
    asyncio.run(run())


def test_limit_includes_decided_but_unpublished_requests():
    async def run():
        p = Publisher("interaction.resolved")
        c = coordinator(p)
        tasks = [asyncio.create_task(c.request(request(str(i)))) for i in range(64)]
        await ready()
        for i in range(64):
            assert await c.submit_interaction(str(i), response())
        await ready()
        with pytest.raises(RuntimeError):
            await c.request(request("overflow"))
        c.begin_cancel()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert len(await c.cleanup("cancelled")) == 64
    asyncio.run(run())


def test_answer_wins_timeout_and_cancel_blocks_new_requests():
    async def run():
        p = Publisher("interaction.resolved")
        c = coordinator(p)
        t = asyncio.create_task(c.request(request(timeout=.01)))
        await ready()
        await c.submit_interaction("i", response())
        await asyncio.sleep(.02)
        p.release.set()
        assert (await t).resolution_reason == "answered"
        c.begin_cancel()
        with pytest.raises(RuntimeError):
            await c.request(request("new"))
    asyncio.run(run())


@pytest.mark.parametrize("purpose,value,decision", [("permission", "deny", "deny"), ("checkpoint", "reject", "rejected"), ("goal", "no", "rejected"), ("loop", "reject", "rejected")])
def test_negative_choice_is_explicit_rejection(purpose, value, decision):
    async def run():
        c = coordinator(NullSemanticEventPublisher(), legacy_auto_approve=True)
        t = asyncio.create_task(c.request(request(purpose=purpose, choices=[dict(label="No", value=value)])))
        await ready()
        assert await c.submit_interaction("i", response(value=value))
        result = await t
        assert result.decision == decision
        assert result.resolution_reason == "user_rejected"
    asyncio.run(run())


def test_scope_is_preserved_and_tool_scope_is_checked():
    async def run():
        c = coordinator(NullSemanticEventPublisher())
        t = asyncio.create_task(c.request(request(input_kind="permission", purpose="permission",
            allowed_scopes=["once", "always"], tools=[dict(name="shell", allowed_scopes=["once"])])))
        await ready()
        with pytest.raises(ValueError):
            await c.submit_interaction("i", response(scope="always"))
        assert await c.submit_interaction("i", response(scope="once"))
        assert (await t).scope == "once"
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_timeout_must_be_finite_positive(timeout):
    assert request().timeout == 120
    with pytest.raises(ValueError):
        request(timeout=timeout)


@pytest.mark.parametrize("block_required", [True, False])
def test_real_full_channel_cancellation(block_required):
    from voidx.agent.application.runtime.semantic_channel import SemanticChannel
    from voidx.agent.domain import semantic_events as e
    from uuid import uuid4

    async def run():
        channel = SemanticChannel(**IDENTITY, capacity=1)
        c = coordinator(channel)
        before = asyncio.all_tasks()
        if block_required:
            await channel.publish(e.InteractionRequired(**IDENTITY, event_id=uuid4(),
                sequence=1, timestamp=0, agent_id=None, parent_tool_call_id=None,
                payload=e.InteractionRequiredPayload(request=request("filler"))))
        t = asyncio.create_task(c.request(request(timeout=.001)))
        await ready()
        if not block_required:
            await c.submit_interaction("i", response())
        await asyncio.sleep(.01)
        assert not t.done()
        c.begin_cancel()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        tail = await c.cleanup("cancelled")
        assert len(tail) == (0 if block_required else 1)
        assert asyncio.all_tasks() == before
    asyncio.run(run())


@pytest.mark.parametrize("value,free_text,decision", [
    ("approved", False, "approved"), ("needs_doc", False, "needs_doc"),
    ("modified", False, "modified"), ("rejected", False, "rejected"),
    ("approved", True, "modified"), ("no", True, "modified"),
])
def test_checkpoint_preserves_actual_decision(value, free_text, decision):
    async def run():
        c = coordinator(Publisher())
        task = asyncio.create_task(c.request(request(purpose="checkpoint", allow_free_text=True,
            choices=[dict(label=v, value=v) for v in ("approved", "needs_doc", "modified", "rejected")])))
        await ready()
        assert await c.submit_interaction("i", response(value=value, free_text=free_text))
        result = await task
        assert result.decision == decision
        assert result.value == value
    asyncio.run(run())


@pytest.mark.parametrize("value", ["approved", "no", "later", "new scope"])
def test_checkpoint_scope_requires_resolved_choice_and_consumes_once(value):
    async def run():
        p = Publisher()
        c = coordinator(p)
        scope = request("scope", purpose="checkpoint", input_kind="text", choices=[],
                        checkpoint_id="i", checkpoint_stage="scope", timeout=.01)
        with pytest.raises(ValueError, match="Checkpoint"):
            await c.request(scope)
        assert not p.events
        task = asyncio.create_task(c.request(request(purpose="checkpoint", checkpoint_id="i",
            checkpoint_stage="decision", choices=[dict(label="Modify", value="modified")])))
        await ready()
        assert await c.submit_interaction("i", response(value="modified"))
        assert (await task).decision == "modified"
        task = asyncio.create_task(c.request(scope))
        await ready()
        assert await c.submit_interaction("scope", response(value=value))
        assert (await task).decision == "modified"
        with pytest.raises(ValueError, match="Checkpoint"):
            await c.request(scope.model_copy(update={"interaction_id": "scope-again"}))
        assert len(p.events) == 4
        assert not c._pending
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["free_text", "dismissed", "timed_out"])
def test_checkpoint_without_modified_choice_cannot_open_scope(ending):
    async def run():
        c = coordinator(Publisher())
        task = asyncio.create_task(c.request(request(purpose="checkpoint", checkpoint_id="i",
            checkpoint_stage="decision", allow_free_text=True, timeout=.01,
            choices=[dict(label="Modify", value="modified")])))
        await ready()
        if ending != "timed_out":
            assert await c.submit_interaction("i", response(value="modified",
                free_text=ending == "free_text", cancelled=ending == "dismissed"))
        await task
        with pytest.raises(ValueError, match="Checkpoint"):
            await c.request(request("scope", purpose="checkpoint", input_kind="text",
                checkpoint_id="i", checkpoint_stage="scope", timeout=.01))
    asyncio.run(run())


@pytest.mark.parametrize("values", [["later"], ["approved", "approved"]])
def test_explicit_checkpoint_model_copy_rejected_before_side_effects(values):
    from pydantic import ValidationError
    from voidx.tooling.domain.ui_events import ChoicePayload

    async def run():
        p = Publisher()
        c = coordinator(p)
        valid = request(purpose="checkpoint", checkpoint_id="i", checkpoint_stage="decision",
                        choices=[dict(label="Approve", value="approved")], timeout=.01)
        invalid = valid.model_copy(update={"choices": [ChoicePayload(label=v, value=v) for v in values]})
        with pytest.raises(ValidationError):
            await c.request(invalid)
        assert p.events == []
        assert c._pending == {} and c._used == set() and c._checkpoint_scopes == set()
        task = asyncio.create_task(c.request(valid))
        await ready()
        assert await c.submit_interaction("i", response(value="approved"))
        assert (await task).decision == "approved"
        assert c._pending == {}
    asyncio.run(run())


def test_checkpoint_capacity_one_resolved_blocked_cancel_has_unique_completion():
    from voidx.agent.application.runtime.semantic_channel import SemanticChannel
    from voidx.agent.application.runtime.run_supervisor import RunSupervisor

    async def run():
        async with asyncio.timeout(5):
            channel = SemanticChannel(**IDENTITY, capacity=1)
            c = coordinator(channel)
            returned = []

            async def execute(supervisor):
                returned.append(await c.request(request(purpose="checkpoint", checkpoint_id="i",
                    checkpoint_stage="decision", choices=[dict(label="Modify", value="modified")])))
                raise AssertionError("Blocked request must not return")

            async def persist(result):
                raise AssertionError("Cancelled run must not persist success")

            managed = RunSupervisor(channel, execute=execute, persist=persist,
                                    cleanup=c.cleanup, before_cancel=c.begin_cancel)
            managed.start()
            while not c._pending or not c._pending["i"].required:
                await asyncio.sleep(0)
            pending = c._pending["i"]
            future = pending.future
            assert channel._queue.full()
            assert await c.submit_interaction("i", response(value="modified"))
            await ready()
            assert channel._lock.locked() and not managed._work.done()
            assert pending.resolution.decision == "modified"
            await managed.cancel()
            events = [event async for event in channel.events()]
            assert [event.kind for event in events] == [
                "interaction.required", "interaction.resolved", "turn.cancelled"]
            assert events[1].payload.interaction_id == "i"
            assert events[1].payload.resolution.decision == "modified"
            assert events[1].payload.resolution.resolution_reason == "answered"
            assert len({event.event_id for event in events}) == 3
            assert [event.sequence for event in events] == [1, 2, 3]
            assert returned == [] and not c._pending and not c._checkpoint_scopes
            assert future.done() and pending.future is None
            assert managed._task.done() and managed._work.done() and channel.completion.done()
            assert not await c.submit_interaction("i", response(value="approved"))
    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["goal", "loop"])
@pytest.mark.parametrize("value,free_text,decision", [
    ("approved", False, "approved"),
    ("revised", False, "revised"),
    ("cancelled", False, "cancelled"),
    ("approved", True, "revised"),
    ("change the goal", True, "revised"),
    ("cancelled", True, "revised"),
    ("rejected", True, "revised"),
    ("Approved", False, "rejected"),
    (" approved ", False, "rejected"),
    ("yes", False, "rejected"),
])
def test_goal_loop_exact_decision(purpose, value, free_text, decision):
    async def run():
        p = Publisher()
        c = coordinator(p)
        t = asyncio.create_task(c.request(request(
            purpose=purpose, allow_free_text=True,
            choices=[dict(label=v, value=v) for v in
                     {"approved", "revised", "cancelled", value}],
        )))
        await ready()
        assert await c.submit_interaction("i", response(value=value, free_text=free_text))
        result = await t
        assert result.decision == decision
        assert result.resolution_reason == "answered"
        assert result.value == value and result.free_text == free_text
        assert p.events[-1].payload.resolution == result
        assert not c._pending
        assert not await c.submit_interaction("i", response(value="approved"))
    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["goal", "loop"])
@pytest.mark.parametrize("invalid", [
    dict(value="unknown"), dict(value="Approved"), dict(value=" approved "),
    dict(session_id="other"), dict(thread_id="other"), dict(turn_id="other"),
    dict(scope="session"), dict(free_text=True),
])
def test_goal_loop_invalid_response_preserves_pending(purpose, invalid):
    async def run():
        p = Publisher()
        c = coordinator(p)
        t = asyncio.create_task(c.request(request(
            purpose=purpose, choices=[dict(label="Approve", value="approved")],
        )))
        await ready()
        pending = c._pending["i"]
        with pytest.raises(ValueError):
            await c.submit_interaction("i", response(**(dict(value="approved") | invalid)))
        assert c._pending["i"] is pending
        assert pending.resolution is None and not pending.future.done()
        assert not t.done() and len(p.events) == 1
        assert await c.submit_interaction("i", response(value="approved"))
        result = await t
        assert result.decision == "approved" and result.resolution_reason == "answered"
        assert len(p.events) == 2 and not c._pending
    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["goal", "loop"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("ending", ["timed_out", "dismissed", "user_rejected"])
def test_goal_loop_fallback_and_legacy(purpose, legacy, ending):
    async def run():
        p = Publisher()
        c = coordinator(p, legacy_auto_approve=legacy)
        t = asyncio.create_task(c.request(request(purpose=purpose, timeout=.01)))
        await ready()
        if ending != "timed_out":
            assert await c.submit_interaction("i", response(
                cancelled=ending == "dismissed", rejected=ending == "user_rejected"))
        result = await t
        expected = "auto_approved" if legacy and ending != "user_rejected" else "rejected"
        assert result.decision == expected and result.resolution_reason == ending
        assert len(p.events) == 2 and not c._pending
        assert not await c.submit_interaction("i", response())
    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["goal", "loop"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("approval_first", [False, True])
def test_goal_loop_approval_cancel_race(purpose, legacy, approval_first):
    async def run():
        p = Publisher()
        c = coordinator(p, legacy_auto_approve=legacy)
        t = asyncio.create_task(c.request(request(
            purpose=purpose, choices=[dict(label="Approve", value="approved")],
        )))
        await ready()
        pending = c._pending["i"]
        future = pending.future
        if approval_first:
            assert await c.submit_interaction("i", response(value="approved"))
        c.begin_cancel()
        assert not await c.submit_interaction("i", response(value="approved"))
        with pytest.raises(asyncio.CancelledError):
            await t
        tail = await c.cleanup("cancelled")
        assert len(p.events) == 1 and len(tail) == 1
        result = tail[0].payload.resolution
        assert result.decision == ("approved" if approval_first else "rejected")
        assert result.resolution_reason == ("answered" if approval_first else "task_cancelled")
        assert future.done() and pending.future is None and not c._pending
        assert await c.cleanup("cancelled") == ()
    asyncio.run(run())


def test_shared_interaction_budget_and_issued_ids_do_not_accumulate_history():
    async def run():
        from voidx.agent.application.runtime.run_ownership import RunOwner
        owner = RunOwner(turns=2, producers=6, interactions=1)
        p = Publisher()
        a = coordinator(p, budget=owner.interactions)
        b = InteractionCoordinator(p, session_id="child", thread_id="child-thread", turn_id="other", budget=owner.interactions)
        first_id = a.issue_id()
        first = asyncio.create_task(a.request(request(first_id)))
        await ready()
        second_id = b.issue_id()
        second = asyncio.create_task(b.request(request(second_id, session_id="child", thread_id="child-thread", turn_id="other")))
        await ready()
        assert len(p.events) == 1
        assert owner.interactions.count == 1
        assert await a.submit_interaction(first_id, response())
        await first
        await ready()
        assert len(p.events) == 3
        assert await b.submit_interaction(second_id, response(session_id="child", thread_id="child-thread", turn_id="other"))
        await second
        assert owner.interactions.count == 0
        for _ in range(1100):
            key = a.issue_id()
            task = asyncio.create_task(a.request(request(key)))
            await ready()
            assert await a.submit_interaction(key, response())
            await task
        assert not a._used
        assert not await a.submit_interaction(first_id, response())
        with pytest.raises(ValueError):
            await a.request(request(first_id))
        await owner.cancel()
    asyncio.run(run())


def test_shared_budget_cancel_rejects_late_approval_before_cleanup():
    async def run():
        from voidx.agent.application.runtime.run_ownership import RunOwner
        owner = RunOwner(interactions=1)
        p = Publisher()
        c = coordinator(p, budget=owner.interactions)
        key = c.issue_id()
        task = owner.spawn(lambda: c.request(request(key, purpose="goal", choices=[dict(label="Approve", value="approved")])))
        await ready()
        owner.begin_cancel()
        assert not await c.submit_interaction(key, response(value="approved"))
        await owner.cancel()
        assert task.done()
        tail = await c.cleanup("cancelled")
        assert len(tail) == 1
        assert tail[0].payload.resolution.decision == "rejected"
        assert owner.interactions.count == 1, "cancel tail retains its global budget until consumed"
    asyncio.run(run())


@pytest.mark.asyncio
async def test_checkpoint_association_retains_global_token_and_scope_transfers_it():
    from voidx.agent.application.runtime.run_ownership import RunOwner
    owner = RunOwner(turns=1, producers=4, interactions=1)
    p = Publisher()
    c = coordinator(p, budget=owner.interactions)
    key = c.issue_id()
    checkpoint_id = key
    task = asyncio.create_task(c.request(request(key, purpose="checkpoint", checkpoint_id=checkpoint_id,
        checkpoint_stage="decision", choices=[dict(label="Modify", value="modified")])))
    await ready()
    assert await c.submit_interaction(key, response(value="modified"))
    await task
    try:
        assert owner.interactions.count == 1, "checkpoint association remains part of global I"
        key = c.issue_id()
        task = asyncio.create_task(c.request(request(key, purpose="checkpoint", input_kind="text", choices=[],
            checkpoint_id=checkpoint_id, checkpoint_stage="scope")))
        await ready()
        assert await c.submit_interaction(key, response(value="new scope"))
        await asyncio.wait_for(task, 1)
        assert owner.interactions.count == 0
    finally:
        c.begin_cancel()
        await owner.cancel()


@pytest.mark.asyncio
async def test_checkpoint_association_cancel_releases_global_token():
    from voidx.agent.application.runtime.run_ownership import RunOwner
    owner = RunOwner(turns=1, producers=4, interactions=1)
    c = coordinator(Publisher(), budget=owner.interactions)
    key = c.issue_id()
    checkpoint_id = key
    task = asyncio.create_task(c.request(request(key, purpose="checkpoint", checkpoint_id=checkpoint_id,
        checkpoint_stage="decision", choices=[dict(label="Modify", value="modified")])))
    await ready()
    await c.submit_interaction(key, response(value="modified"))
    await task
    assert owner.interactions.count == 1
    c.begin_cancel()
    c.begin_cancel()
    assert owner.interactions.count == 0
    await owner.cancel()

@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_tail", [False, True])
async def test_failed_cleanup_reclaims_interaction_tokens(invalid_tail):
    from voidx.agent.application.runtime.run_ownership import RunOwner
    owner = RunOwner(turns=1, producers=4, interactions=1)
    entered = asyncio.Event()
    holder = []
    async def execute(supervisor):
        c = InteractionCoordinator(supervisor.channel, **supervisor.channel.identity, budget=owner.interactions)
        holder.append(c)
        entered.set()
        await c.request(InteractionRequest(**supervisor.channel.identity, interaction_id=c.issue_id(),
                        input_kind="choice", purpose="generic", prompt="Choose", choices=[dict(label="Yes", value="yes")]))
    async def persist(result):
        pass
    async def cleanup(outcome):
        if invalid_tail:
            tail = await holder[0].cleanup(outcome)
            return [tail[0].model_copy(update={"session_id": "wrong"})]
        raise ValueError("cleanup failed")
    await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
    await entered.wait()
    await ready()
    assert owner.interactions.count == 1
    if invalid_tail:
        await owner.cancel()
    else:
        with pytest.raises(ExceptionGroup, match="Owned run cleanup failed") as raised:
            await owner.cancel()
        assert len(raised.value.exceptions) == 1
        error = raised.value.exceptions[0]
        assert isinstance(error, ValueError)
        assert str(error) == "cleanup failed"
    with pytest.raises(RuntimeError, match="Run failed"):
        _ = [item async for item in owner.events()]
    assert owner.interactions.count == 0
    assert not holder[0]._pending
