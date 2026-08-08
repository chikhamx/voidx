"""Tests for turn control metrics sink."""

from voidx.agent.adapters.langgraph.runtime.turn_metrics import TurnControlMetrics


def test_metrics_start_at_zero():
    m = TurnControlMetrics()
    snapshot = m.snapshot()
    assert snapshot["turn_control_called"] == 0
    assert snapshot["turn_control_missing"] == 0
    assert snapshot["turn_control_invalid"] == 0
    assert snapshot["turn_control_mixed_tools"] == 0
    assert snapshot["turn_control_first_prompt"] == 0
    assert snapshot["turn_control_second_prompt"] == 0
    assert snapshot["turn_control_prompt_succeeded"] == 0
    assert snapshot["turn_control_third_miss_fallback"] == 0


def test_increment_called():
    m = TurnControlMetrics()
    m.increment("turn_control_called")
    assert m.snapshot()["turn_control_called"] == 1


def test_increment_missing():
    m = TurnControlMetrics()
    m.increment("turn_control_missing")
    m.increment("turn_control_missing")
    assert m.snapshot()["turn_control_missing"] == 2


def test_increment_all_counters():
    m = TurnControlMetrics()
    m.increment("turn_control_called")
    m.increment("turn_control_missing")
    m.increment("turn_control_invalid")
    m.increment("turn_control_mixed_tools")
    m.increment("turn_control_first_prompt")
    m.increment("turn_control_second_prompt")
    m.increment("turn_control_prompt_succeeded")
    m.increment("turn_control_third_miss_fallback")
    snap = m.snapshot()
    for v in snap.values():
        assert v == 1


def test_increment_unknown_counter_is_noop():
    m = TurnControlMetrics()
    m.increment("nonexistent_counter")
    snap = m.snapshot()
    assert all(v == 0 for v in snap.values())


def test_snapshot_returns_copy():
    m = TurnControlMetrics()
    m.increment("turn_control_called")
    snap = m.snapshot()
    snap["turn_control_called"] = 999
    assert m.snapshot()["turn_control_called"] == 1


def test_reset():
    m = TurnControlMetrics()
    m.increment("turn_control_called", 3)
    m.increment("turn_control_missing", 2)
    m.reset()
    snap = m.snapshot()
    assert all(v == 0 for v in snap.values())


def test_increment_with_count():
    m = TurnControlMetrics()
    m.increment("turn_control_called", 5)
    assert m.snapshot()["turn_control_called"] == 5
