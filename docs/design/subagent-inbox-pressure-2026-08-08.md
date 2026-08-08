# Subagent Inbox Pressure & Review Channel Structuring

Date: 2026-08-08

> **Status: Design draft, awaiting review**
>
> This document investigates a recurring failure mode where long-running
> review/inspect subagents fail with `AgentGatewayError("Inbox is full")`,
> and proposes a structured solution so the parent run can observe
> progress without blocking on terminal state.

## 1. Problem

Review and inspect subagents in the current session
(`0642997eb884`, 2026-08-08 11:47) failed with the following error
reported to the parent:

```text
Lumen(确认 P6 config zero-import 批次可进入全套验证) failed (error, Inbox is full, 331.3s)
Wait("Lumen") (293.4s) Lumen failed (terminal_reached_during_wait)

完整 focused 集合 1112 项通过，编译与 diff 格式通过。等待复审结果；若 PASS，立即执行后端全套。
独立复审通道因容量限制未返回结论，不是代码失败；上一轮 blocker 均已有代码与测试证据关闭。

Lyra(最终确认 P6 config zero-import 批次) failed (error, Inbox is full, 281.3s)
```

The same error pattern has appeared across several sessions in the last
week (e.g. `417d7b1d2b56` and `7c3d2b776f7c`), confirming it is not a
one-off.

## 2. Evidence Trail

### 2.1 Code-level source of the error

`src/voidx/agent/adapters/subagent/inprocess_gateway.py:296-305`:

```python
async def _put_message(self, record: _RunRecord, message: AgentMessage, *, lifecycle: bool = False) -> None:
    if record.inbox.full():
        if lifecycle:
            try:
                record.inbox.get_nowait()
            except asyncio.QueueEmpty:
                pass
        else:
            raise AgentGatewayError("Inbox is full")
    await record.inbox.put(message)
```

`inbox_capacity` defaults to `100`. The queue is the **target run's
inbox**: `send()` calls `_put_message(target, message)` (line 145), so
when a child sends any non-lifecycle message (`progress`, `artifact`,
`result`, `log`, etc.) to its parent, the parent's queue is what fills.

### 2.2 Where the string actually appears in logs

A grep across all `.voidx/sessions/*` shows two distinct populations of
matches:

1. **Tool result bodies** — child agents reading
   `src/voidx/agent/gateway/gateway.py` (the pre-refactor path) or its
   successor `src/voidx/agent/adapters/subagent/inprocess_gateway.py`.
   These are plain source-code fragments seen by `read` tool results,
   e.g. `417d7b1d2b56/messages.jsonl:364`,
   `7c3d2b776f7c/subagents/run_195b58b8...:24/375/376/392/399/404`. They
   are not runtime errors.
2. **Live subagent finishes** — `7c3d2b776f7c/subagents/run_335b8b4...`
   has a child finishing with
   `{ok: false, finish_reason: "error", elapsed: 281.28}` and no
   `error` payload persisted. Same shape as the screenshot, which means
   the runtime captures the error in memory and forwards it to the
   parent before the child's `subagent_finish` event is flushed.

The current session `0642997eb884` shows no fresh child jsonl in
`subagents/` and no `Inbox is full` text in `messages.jsonl` after the
04:07 user turn — confirming the failing children are in-flight and
their finish events have not yet been written.

### 2.3 Why only review/inspect children fail

The trigger condition for raising `Inbox is full` is:
`(target inbox is full) AND (message is non-lifecycle)`.

A child `progress` message is non-lifecycle. The target is its parent.
The parent only drains its own inbox when it calls
`receive()`/`wait()`. While the parent is blocked on `wait()` for the
child's terminal status, it does not drain `progress`.

For review/inspect children:
- They run long (200s–1000s), generating many progress / step events.
- Their parent is **the orchestrator that spawned them**, which spends
  most of its time on `wait()` (terminal-state wait).
- Result: parent's inbox fills, child raises on its next `send()`, the
  child runner's `except Exception` marks the run `failed`, and the
  parent's `wait()` reports `terminal_reached_during_wait`.

Implementation-side children (`tdd`, `implement`) do not hit this as
often because:
- They produce fewer progress messages per second.
- The orchestrator interrupts their `wait()` to consume progress
  between `wait` and the next user action.

## 3. Why Current Tests Miss It

`src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py:322`
explicitly asserts the throw:

```python
with pytest.raises(AgentGatewayError, match="Inbox is full"):
    await gateway.send(...)
```

This pins the current behavior as correct, so any change must either
update this test or add a new one covering the desired behavior.

## 4. Design Goals

1. Review/inspect children that emit progress must not fail because
   their parent is busy in `wait()`.
2. Parents must still see live progress (so the UI can render it) — we
   are not allowed to drop messages silently.
3. Lifecycle messages (`completed`/`failed`/`cancelled`) must remain
   authoritative and never be coalesced away.
4. The behavior must be testable without a live parent-child race.

## 5. Proposed Solution

### 5.1 Two changes, layered

**Change A — back-pressure in `_put_message`** (the small, decisive fix)

Replace the immediate raise with bounded back-pressure:

```python
async def _put_message(self, record, message, *, lifecycle=False, put_timeout=1.0):
    if lifecycle:
        # lifecycle always wins: drop oldest non-lifecycle if full
        while record.inbox.full():
            try:
                record.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
        await record.inbox.put(message)
        return
    # non-lifecycle: wait briefly for the consumer to drain
    try:
        await asyncio.wait_for(record.inbox.put(message), timeout=put_timeout)
    except asyncio.TimeoutError as exc:
        raise AgentGatewayError("Inbox is full") from exc
```

Effects:
- Progress messages naturally throttle the child to the parent's
  drain rate.
- A genuine consumer death (parent hung) still surfaces as an error
  after `put_timeout`, just like today.

**Change B — structured progress for review/inspect** (the protocol fix)

Today, every step a child takes is `progress`/`artifact`. We
introduce a typed progress envelope so the parent can decide:

```python
class ProgressLevel(str, Enum):
    TRACE = "trace"      # coalesced / dropped freely
    INFO  = "info"       # coalesced, keep last N
    MILESTONE = "milestone"  # always delivered, never coalesced

@dataclass
class ProgressEvent:
    level: ProgressLevel
    step: str
    payload: dict[str, Any]
```

Children that are review/inspect default to `INFO` (coalesced) or
`MILESTONE` (delivered) for natural step boundaries. The parent
delivers only `MILESTONE` events to the UI by default; `INFO` is
coalesced into the latest of the same `step`.

Effects:
- The inbox no longer fills up just because the child narrates each
  file read.
- The UI still sees real checkpoints (`MILESTONE`).
- `Trace`-level events are dropped when the inbox is under pressure
  (lifecycle=true behavior), preserving today's overflow policy at the
  bottom layer.

### 5.2 Putting it together

Order of operations in the new world:

1. Child `send(progress, level=INFO, step="review.feed_id")` →
   `_put_message` waits up to 1s for parent drain; if parent is
   waiting on terminal, the parent-side `_wait()` loop
   (`receive(timeout=0.5)`) keeps the inbox below 100.
2. Child `send(result, ...)` is unchanged: it is always delivered, and
   the child is closed after.
3. `subagent_finish` event already uses lifecycle channel; the new
   `_put_message` keeps the "drop oldest" fallback for it.

## 6. Files Touched

- `src/voidx/agent/adapters/subagent/inprocess_gateway.py`
  - `_put_message`: add bounded back-pressure.
  - New `ProgressLevel` enum + `ProgressEvent` dataclass in
    `src/voidx/agent/domain/subagent.py`.
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
  - Wrap child `progress` payloads in `ProgressEvent(level=INFO)`.
  - Add a public child-side helper `child.report_milestone(step, payload)`
    that maps to `send(level=MILESTONE)`.
- `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`
  - When waiting on a child, drive `gateway.receive(timeout=0.5)` in a
    loop to keep the inbox drained.
- `src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py`
  - Update the `test_inbox_full_rejects_regular_message_but_keeps_lifecycle`
    test to assert the new back-pressure semantics: under a slow
    consumer, `send` waits and succeeds once the consumer drains.
  - Add `test_progress_coalescing_drops_trace_keeps_milestone`.
  - Add `test_lifecycle_still_drops_oldest_under_pressure`.

## 7. Risks & Non-Goals

- **Risk**: Increasing `put_timeout` past the child LLM step cadence
  could cause child progress to lag visibly. Default 1.0s is a
  compromise between throttling and liveness. Configurable per-gateway.
- **Risk**: Coalescing `INFO` events could hide useful state from the
  UI. Mitigation: parents can opt into `raw_progress=True` for
  debugging sessions.
- **Non-goal**: This design does not change `subagent_capacity` (max
  concurrent children) or `max_payload_bytes` (existing 65536). They
  are orthogonal.
- **Non-goal**: We do not redesign `wait()`. The existing
  `terminal_reached_during_wait` semantics stay.

## 8. Verification Plan

1. **Unit**: new tests in
   `src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py`
   must pass: `./test.py --backend -- src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py`.
2. **Targeted regression**: `./test.py --backend -- src/tests/test_agent`
   must stay green.
3. **Full backend**: `./test.py --backend`.
4. **Manual scenario** (mirrors the failing session):
   - Spawn a review child with 250 mocked `INFO` progress messages.
   - Parent only calls `wait()`.
   - With old code: child fails with `Inbox is full` at message 101.
   - With new code: child finishes cleanly; parent's
     `receive(timeout=0.5)` loop drains the inbox throughout.

## 9. Out of Scope

The "独立复审通道因容量限制未返回结论" sentence in the screenshot is
the parent-side observation that caused the orchestrator to skip its
own test suite the first time around. The fix in §5 unblocks that
flow, but the orchestrator's policy on "how many review children may
run in parallel before the parent test suite is allowed to proceed"
remains an orchestrator-level decision and is tracked separately.