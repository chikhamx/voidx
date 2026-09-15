"""Supervisor termination, task ownership and completion ordering."""
import asyncio
import json
from uuid import uuid4

import pytest

from voidx.agent.domain import semantic_events as e
from voidx.agent.application.runtime.semantic_channel import SemanticChannel
from voidx.agent.application.runtime.run_supervisor import RunResult, RunSupervisor


def channel(**kwargs):
    return SemanticChannel(session_id="s", thread_id="t", turn_id="r", **kwargs)


def event(kind="assistant.chunk", payload=None):
    data = dict(event_id=str(uuid4()), session_id="s", thread_id="t", turn_id="r",
                sequence=99, agent_id=None, parent_tool_call_id=None, timestamp=0.0,
                kind=kind, payload=payload or {"stream_id": "a", "phase": "text", "delta": "hi"})
    return e.decode_event(json.dumps(data).encode())


def result():
    return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}))


async def persist(_result):
    pass


async def cleanup(_outcome):
    return ()


def test_completed_only_after_persistence_and_cleanup():
    async def run():
        c = channel(capacity=1)
        order = []
        async def execute(owner):
            await owner.channel.publish(event())
            return result()
        async def save(value):
            assert isinstance(value, RunResult)
            assert not c.completion.done()
            order.append("persist")
        async def clean(outcome):
            assert not c.completion.done()
            assert outcome == "completed"
            order.append("cleanup")
            return ()
        owner = RunSupervisor(c, execute=execute, persist=save, cleanup=clean)
        owner.start()
        await asyncio.wait_for(owner.wait(), 1)
        assert order == ["persist", "cleanup"]
        output = [item async for item in owner.events()]
        assert [item.kind for item in output] == ["assistant.chunk", "turn.completed"]
        assert [item.sequence for item in output] == [1, 2]
        with pytest.raises(RuntimeError):
            await c.publish(event())
        await owner.cancel()
        assert [item async for item in owner.events()] == []
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["execute", "persist", "cleanup", "child", "oversize"])
def test_failure_wakes_empty_queue_and_is_redacted(failure):
    async def run():
        c = channel(max_event_bytes=1024)
        cleaned = []
        async def broken():
            raise RuntimeError("SECRET credential traceback")
        async def execute(owner):
            if failure == "execute":
                await broken()
            if failure == "child":
                owner.spawn(broken())
                await asyncio.Event().wait()
            if failure == "oversize":
                await c.publish(event(payload={"stream_id": "a", "phase": "text", "delta": "x" * 2000}))
            return result()
        async def save(value):
            if failure == "persist":
                await broken()
        async def clean(outcome):
            cleaned.append(outcome)
            if failure == "cleanup":
                await broken()
            return ()
        owner = RunSupervisor(c, execute=execute, persist=save, cleanup=clean)
        before = asyncio.all_tasks()
        async def collect():
            return [item async for item in owner.events()]
        output = await asyncio.wait_for(collect(), 1)
        assert len(output) == 1 and output[0].kind == "turn.failed"
        assert "SECRET" not in e.encode_event(output[0]).decode()
        assert cleaned
        assert asyncio.all_tasks() == before
    asyncio.run(run())


@pytest.mark.parametrize("tail_count", [0, 64, 65])
def test_full_queue_repeated_cancel_tail_bounds_and_no_leaks(tail_count):
    async def run():
        c = channel(capacity=1)
        blocked = asyncio.Event()
        stopped = []
        async def child():
            try:
                await c.publish(event())
            finally:
                stopped.append("child")
        async def execute(owner):
            await c.publish(event())
            owner.spawn(child())
            blocked.set()
            try:
                await c.publish(event())
            finally:
                stopped.append("execute")
        async def clean(outcome):
            assert owner.cancelling and len(stopped) == 2
            assert outcome == "cancelled"
            return tuple(event("interaction.resolved", {
                "interaction_id": str(i), "purpose": "generic",
                "resolution": {"value": "", "free_text": False, "decision": "cancelled", "resolution_reason": "task_cancelled"},
            }) for i in range(tail_count))
        before = asyncio.all_tasks()
        owner = RunSupervisor(c, execute=execute, persist=persist, cleanup=clean)
        owner.start()
        await blocked.wait()
        await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.gather(owner.cancel(), owner.cancel()), 1)
        output = [item async for item in owner.events()]
        assert output[0].kind == "assistant.chunk"
        if tail_count <= 64:
            assert len(output) == tail_count + 2
            assert output[-1].kind == "turn.cancelled"
        else:
            assert len(output) == 2
            assert output[-1].kind == "turn.failed"
        assert [item.sequence for item in output] == list(range(1, len(output) + 1))
        assert asyncio.all_tasks() == before
    asyncio.run(run())


def test_iterator_close_stops_producers():
    async def run():
        before = asyncio.all_tasks()
        async def execute(owner):
            while True:
                await owner.channel.publish(event())
        owner = RunSupervisor(channel(capacity=1), execute=execute, persist=persist, cleanup=cleanup)
        iterator = owner.events()
        await anext(iterator)
        await iterator.aclose()
        assert owner.channel.completion.done()
        assert asyncio.all_tasks() == before
    asyncio.run(run())


def test_cancel_immediately_after_start():
    async def run():
        async def execute(owner):
            await asyncio.Event().wait()
        owner = RunSupervisor(channel(), execute=execute, persist=persist, cleanup=cleanup)
        owner.start()
        await asyncio.wait_for(owner.cancel(), 1)
        assert [item.kind async for item in owner.events()] == ["turn.cancelled"]
    asyncio.run(run())


def test_coordinator_only_returns_tail_for_successfully_published_required():
    async def run():
        c = channel(capacity=1)
        accepted = []
        waiting = asyncio.Event()
        async def execute(owner):
            for interaction_id in ("accepted", "blocked"):
                if interaction_id == "blocked":
                    waiting.set()
                await c.publish(event("interaction.required", {"request": {
                    "interaction_id": interaction_id, "session_id": "s", "thread_id": "t", "turn_id": "r",
                    "input_kind": "text", "purpose": "generic", "prompt": "Input?",
                }}))
                accepted.append(interaction_id)
            return result()
        async def clean(outcome):
            return tuple(event("interaction.resolved", {
                "interaction_id": key, "purpose": "generic",
                "resolution": {"decision": "cancelled", "resolution_reason": "task_cancelled"},
            }) for key in accepted)
        owner = RunSupervisor(c, execute=execute, persist=persist, cleanup=clean)
        owner.start()
        await asyncio.wait_for(waiting.wait(), 1)
        await asyncio.wait_for(owner.cancel(), 1)
        output = [item async for item in owner.events()]
        assert [item.kind for item in output] == ["interaction.required", "interaction.resolved", "turn.cancelled"]
        assert output[1].payload.interaction_id == "accepted"
        assert accepted == ["accepted"]
    asyncio.run(run())


@pytest.mark.parametrize(("failure", "context", "error_type"), [
    ("execute", "run_supervisor_execute_persist", "ExceptionGroup"),
    ("persist", "run_supervisor_execute_persist", "RuntimeError"),
    ("child", "run_supervisor_execute_persist", "ExceptionGroup"),
    ("cleanup", "run_supervisor_cleanup", "RuntimeError"),
    ("cleanup_cancel", "run_supervisor_cleanup", "CancelledError"),
    ("finish_value", "run_supervisor_finish", "ValueError"),
    ("finish_type", "run_supervisor_finish", "TypeError"),
])
def test_internal_diagnostics_without_sdk_backpressure_or_public_secrets(
    failure, context, error_type, monkeypatch, tmp_path,
):
    from voidx.observability import log_internal_error

    monkeypatch.setitem(log_internal_error.__kwdefaults__, "log_dir", tmp_path)
    secret = "SECRET credential traceback"

    async def run():
        c = channel(capacity=1)
        error = (asyncio.CancelledError(secret) if failure == "cleanup_cancel"
                 else RuntimeError(secret))

        async def broken():
            raise error

        async def execute(owner):
            await c.publish(event())
            if failure == "execute":
                await broken()
            if failure == "child":
                owner.spawn(broken())
            return result()

        async def save(value):
            if failure == "persist":
                await broken()

        async def clean(outcome):
            if failure in {"cleanup", "cleanup_cancel"}:
                await broken()
            return ()

        if failure.startswith("finish_"):
            finish = c._finish

            def broken_finish(terminal, tail=()):
                if terminal.kind == "turn.completed":
                    if failure == "finish_value":
                        raise ValueError(secret)
                    raise TypeError(secret)
                return finish(terminal, tail)

            monkeypatch.setattr(c, "_finish", broken_finish)

        before = asyncio.all_tasks()
        owner = RunSupervisor(c, execute=execute, persist=save, cleanup=clean)
        # No consumer until both logging and completion finish with a full queue.
        await asyncio.wait_for(owner.wait(), 1)
        assert c.completion.done()
        log_path = tmp_path / "internal_error.jsonl"
        assert log_path.exists(), "Suppressed failure must have internal diagnostics"
        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["event"] == "internal_error"
        assert entry["context"] == context
        assert entry["session_id"] == "s"
        assert entry["error_type"] == error_type
        assert secret in entry["traceback"]
        assert "test_run_supervisor.py" in entry["traceback"]
        if error_type != "ExceptionGroup":
            assert entry["error_message"] == secret

        output = [item async for item in owner.events()]
        assert [item.kind for item in output] == ["assistant.chunk", "turn.failed"]
        assert output[-1].payload == e.DiagnosticPayload(
            code="run_failed", summary="Run failed", recoverable=False,
        )
        assert all(secret not in e.encode_event(item).decode() for item in output)
        assert asyncio.all_tasks() == before

    asyncio.run(run())


@pytest.mark.parametrize("capacity", [1, 2])
def test_cancel_seals_interactions_before_stopping_producers(capacity):
    from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
    from voidx.tooling.domain.interaction import InteractionRequest, InteractionResponse

    async def run():
        c = channel(capacity=capacity)
        interactions = InteractionCoordinator(c, **c.identity, legacy_auto_approve=True)
        entered = asyncio.Event()
        decisions = []
        async def execute(owner):
            try:
                entered.set()
                await interactions.request(InteractionRequest(**c.identity, interaction_id="goal",
                    input_kind="choice", purpose="goal", prompt="Proceed?"))
            finally:
                decisions.append(await interactions.submit_interaction("goal", InteractionResponse(
                    **c.identity, value="", cancelled=True)))
            return result()
        owner = RunSupervisor(c, execute=execute, persist=persist,
                              cleanup=interactions.cleanup, before_cancel=interactions.begin_cancel)
        owner.start()
        await entered.wait()
        await asyncio.sleep(0)
        await asyncio.wait_for(owner.cancel(), 1)
        assert decisions == [False]
        output = [item async for item in owner.events()]
        assert [x.kind for x in output] == ["interaction.required", "interaction.resolved", "turn.cancelled"]
        assert output[1].payload.resolution.decision == "rejected"
    asyncio.run(run())


@pytest.mark.parametrize("timing", ["running", "before_drive", "before_start"])
def test_before_cancel_failure_stops_work_and_reports_failed(timing, monkeypatch, tmp_path):
    from voidx.observability import log_internal_error

    monkeypatch.setitem(log_internal_error.__kwdefaults__, "log_dir", tmp_path)
    secret = "private cancel hook failure"

    async def run():
        before = asyncio.all_tasks()
        entered = asyncio.Event()
        child_entered = asyncio.Event()
        stopped = []
        cleaned = []
        hook_calls = []

        async def child():
            child_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append("child")

        async def execute(owner):
            entered.set()
            owner.spawn(child())
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append("execute")

        async def clean(outcome):
            cleaned.append(outcome)
            assert sorted(stopped) == (["child", "execute"] if timing == "running" else [])
            return ()

        def before_cancel():
            hook_calls.append(True)
            raise RuntimeError(secret)

        owner = RunSupervisor(channel(), execute=execute, persist=persist,
                              cleanup=clean, before_cancel=before_cancel)

        async def cancel_at_requested_time():
            if timing != "before_start":
                owner.start()
            if timing == "running":
                await entered.wait()
                await child_entered.wait()
            else:
                assert owner._work is None
            # Direct await ensures cancellation precedes the newly scheduled drive.
            await owner.cancel()

        await asyncio.wait_for(cancel_at_requested_time(), 1)
        await asyncio.wait_for(asyncio.gather(owner.cancel(), owner.cancel()), 1)
        assert hook_calls == [True]
        assert cleaned == ["failed"]
        assert entered.is_set() == (timing == "running")
        assert owner._task.done()
        assert owner._work is None or owner._work.done()
        output = [item async for item in owner.events()]
        assert [item.kind for item in output] == ["turn.failed"]
        assert output[0].payload == e.DiagnosticPayload(
            code="run_failed", summary="Run failed", recoverable=False,
        )
        assert secret not in e.encode_event(output[0]).decode()
        assert asyncio.all_tasks() == before
        entries = [json.loads(line) for line in
                   (tmp_path / "internal_error.jsonl").read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["context"] == "run_supervisor_before_cancel"
        assert entries[0]["session_id"] == "s"
        assert entries[0]["error_type"] == "RuntimeError"
        assert entries[0]["error_message"] == secret
        assert secret in entries[0]["traceback"]

    asyncio.run(run())
