# LLM Two-Phase Retry Strategy

Date: 2026-07-09

> **Status: Done** — Archived on 2026-07-10.

## Goal

Replace the linear backoff retry strategy in the main LLM call path with a
two-phase strategy: a fixed-delay fast-reconnect phase followed by an
exponential backoff phase with a cap.

## Current State

Two call sites implement LLM retry with hand-written loops:

- `src/voidx/agent/graph/core/llm.py:296` — main agent LLM call (`_call_llm`)
- `src/voidx/agent/graph/subagent.py:55` — subagent LLM call (`run_subagent`)

Both use the same pattern:

```python
max_retries = 5  # or _LLM_MAX_RETRIES = 5
delay = failed_attempts * 2  # linear: 2, 4, 6, 8, 10s
```

This gives 6 total attempts with delays of 2s, 4s, 6s, 8s, 10s (worst-case
30s wait). The linear backoff is too aggressive in the early phase (a
transient blip shouldn't wait 4s) and too conservative in the late phase
(a prolonged outage recovers but we've already given up after 5 retries).

### Not changed

- `src/voidx/tools/retry.py` (`retry_async` + `RetryConfig`) — used by
  goal_resolver, webfetch, MCP client. Stays as-is.
- `src/voidx/agent/graph/compaction_coordinator.py` — compaction retry loop.
  Stays as-is.

## Design

### Two-phase delay schedule

| Phase | Retry # | Delay |
|-------|---------|-------|
| First (fixed) | 1 | 2s |
| First (fixed) | 2 | 2s |
| Second (exponential ×2, cap 60s) | 3 | 2s |
| Second | 4 | 4s |
| Second | 5 | 8s |
| Second | 6 | 16s |
| Second | 7 | 32s |
| Second | 8 | 60s (capped) |
| Second | 9 | 60s (capped) |
| Second | 10 | 60s (capped) |

- Total attempts: 11 (1 initial + 10 retries)
- Worst-case total wait: 2 + 2 + 2 + 4 + 8 + 16 + 32 + 60 + 60 + 60 = 246s (~4min)

### Delay calculation

A shared helper function in `src/voidx/agent/graph/core/helpers.py`:

```python
_LLM_MAX_RETRIES = 10
_LLM_RETRY_FIXED_PHASE = 2       # first N retries use fixed delay
_LLM_RETRY_FIXED_DELAY = 2.0     # seconds
_LLM_RETRY_BASE_DELAY = 2.0      # exponential base
_LLM_RETRY_MAX_DELAY = 60.0      # cap

def _llm_retry_delay(attempt: int) -> float:
    """Return delay in seconds for the given 1-based retry attempt number."""
    if attempt <= _LLM_RETRY_FIXED_PHASE:
        return _LLM_RETRY_FIXED_DELAY
    exp = attempt - _LLM_RETRY_FIXED_PHASE - 1  # 0, 1, 2, ...
    return min(_LLM_RETRY_BASE_DELAY * (2 ** exp), _LLM_RETRY_MAX_DELAY)
```

### Changes per file

#### `src/voidx/agent/graph/core/helpers.py`

- Add `_LLM_MAX_RETRIES = 10` constant (replaces inline `max_retries = 5` in llm.py)
- Add `_llm_retry_delay(attempt)` function
- Add the phase/delay constants above

#### `src/voidx/agent/graph/core/llm.py`

- Import `_LLM_MAX_RETRIES` and `_llm_retry_delay` from helpers
- Replace `max_retries = 5` with `max_retries = _LLM_MAX_RETRIES`
- Replace `delay = failed_attempts * 2` with `delay = _llm_retry_delay(failed_attempts)`
- Everything else unchanged: error classification, malformed tool call retry,
  context overflow compaction retry, UI status events

#### `src/voidx/agent/graph/subagent.py`

- Import `_LLM_MAX_RETRIES` and `_llm_retry_delay` from helpers
- Remove local `_LLM_MAX_RETRIES = 5`
- Replace `delay = llm_failed_attempts * 2` with `delay = _llm_retry_delay(llm_failed_attempts)`
- Everything else unchanged

### Error classification (unchanged)

The existing `_classify_llm_error` logic in `helpers.py` remains as-is:

- `NON_RETRYABLE` (400/401/403/404) → fail immediately, no retry
- `CONTEXT_OVERFLOW` → compaction retry (llm.py only; subagent raises)
- `RATE_LIMIT` / `SERVER_ERROR` / `TIMEOUT` / `NETWORK` / `UNKNOWN` → retry

## Testing

### Existing tests to update

- `src/tests/test_agent/graph/test_subagent_llm_retry.py`:
  - `test_run_subagent_retries_transient_llm_errors_and_cleans_retry_status`:
    asserts `sleep_delays == [2, 4]` for 2 retries. With the new schedule,
    2 retries still produce `[2, 2]` (both in fixed phase). Update assertion.
  - `test_run_subagent_exhausts_retryable_llm_errors`: asserts
    `attempts == 6` and `sleep_delays == [2, 4, 6, 8, 10]`. With the new
    schedule, update to `attempts == 11` and
    sleep_delays == [2, 2, 2, 4, 8, 16, 32, 60, 60, 60].
    Also update the retry_events assertion: the test currently asserts
    5 `StatusUpdated` + 1 `StatusFinished`. With 10 retries, update to
    10 `StatusUpdated` + 1 `StatusFinished`.

- `src/tests/test_agent/graph/test_call_llm_compaction_advanced.py`:
  - `test_call_llm_retries_five_times_then_renders_assistant_error_without_state_message`:
  asserts `calls == 6`, 5 retry events with delays
  `[2, 4, 6, 8, 10]`, and failure message "LLM call failed after 6 attempts".
  Update to `calls == 11`, 10 retry events with delays
  `[2, 2, 2, 4, 8, 16, 32, 60, 60, 60]`, and failure message
  "LLM call failed after 11 attempts". The `AlwaysFailsStreamingModel`
  already numbers errors by call count, so the expected retry details become
  "retrying in 2s: Connection error 1." through "retrying in 60s: Connection error 10.".
  Rename to `test_call_llm_exhausts_retries_then_renders_assistant_error`
  (the current name says "five times" which no longer applies).

### New tests

- `test_llm_retry_delay` (add to `src/tests/test_agent/graph/test_call_llm_compaction.py`):
  unit test the delay function for all 10 attempts,
  verifying fixed phase (2, 2) and exponential phase (2, 4, 8, 16, 32, 60, 60, 60).

### Verification command

```bash
./test.py --backend -- src/tests/test_agent/graph/test_subagent_llm_retry.py src/tests/test_agent/graph/test_call_llm_compaction_advanced.py src/tests/test_agent/graph/test_call_llm_compaction.py -v
```

## Risks

- **Worst-case wait time**: 246s (~4min) if all 10 retries fail. This is
  intentional — the strategy favors persistence over fast failure for
  transient outages. The cap at 60s prevents unbounded growth.
- **Test speed**: Exhausts-retry tests must mock `asyncio.sleep` to avoid
  real waiting. Existing tests already do this; new tests must follow suit.
- **Subagent behavior divergence**: Subagent treats `CONTEXT_OVERFLOW` as
  non-retryable (raises immediately). This is unchanged by this spec.
