from dataclasses import FrozenInstanceError

import pytest

from voidx.agent.application.task_state_reminder import (
    TaskStateReminderPolicy,
    normalize_snapshot,
)


def prepare(policy, **kwargs):
    return policy.prepare(snapshot=normalize_snapshot({"goal": "ship"}), **kwargs)


def accept(policy, decision):
    return policy.commit(decision, successful=True, accepted=True)


def test_prepare_is_pure_and_commit_requires_success_and_acceptance():
    policy = TaskStateReminderPolicy()
    baseline = policy.state
    decision = prepare(policy, semantic_tokens=100, snapshot_tokens=12)
    assert decision.reason == "initial" and decision.append_snapshot
    assert decision.semantic_tokens == 100 and decision.snapshot_tokens == 12
    assert decision.baseline == baseline
    assert prepare(policy, semantic_tokens=100).reason == "initial"
    assert policy.state == baseline
    for successful, accepted in [(False, False), (False, True), (True, False)]:
        assert not policy.commit(decision, successful=successful, accepted=accepted)
        assert policy.state == baseline
    assert accept(policy, decision)
    assert not accept(policy, decision)
    assert policy.state.calls_since_snapshot == 0
    assert policy.state.semantic_tokens_at_snapshot == 100
    with pytest.raises(FrozenInstanceError):
        decision.reason = "unchanged"
    with pytest.raises(FrozenInstanceError):
        policy.state.calls_since_snapshot = 99


def test_default_call_interval_request_seven_and_duplicate_commit():
    policy = TaskStateReminderPolicy()
    accept(policy, prepare(policy, semantic_tokens=0))
    for count in range(1, 6):
        decision = prepare(policy, semantic_tokens=count)
        assert decision.reason == "unchanged" and not decision.append_snapshot
        assert accept(policy, decision)
        assert not accept(policy, decision)
        assert policy.state.calls_since_snapshot == count
    decision = prepare(policy, semantic_tokens=6)
    assert decision.reason == "call_interval"
    accept(policy, decision)
    assert policy.state.calls_since_snapshot == 0


@pytest.mark.parametrize("delta,reason", [(8191, "unchanged"), (8192, "token_interval"), (10000, "token_interval")])
def test_token_delta_includes_current_request_without_accumulating(delta, reason):
    policy = TaskStateReminderPolicy()
    accept(policy, prepare(policy, semantic_tokens=100))
    for _ in range(3):
        decision = prepare(policy, semantic_tokens=100 + delta)
        assert decision.reason == reason
        assert decision.new_semantic_tokens == delta
    assert policy.state.semantic_tokens_at_snapshot == 100


@pytest.mark.parametrize("kwargs,reason", [
    ({"anchor_valid": False, "user_input_ids": ("u",)}, "history_reset"),
    ({"user_input_ids": ("u",), "task_input_ids": ("t",)}, "user_message"),
    ({"task_input_ids": ("t",)}, "task_message"),
    ({}, "state_changed"),
])
def test_trigger_priority(kwargs, reason):
    policy = TaskStateReminderPolicy(call_interval=1, token_interval=1)
    accept(policy, prepare(policy, semantic_tokens=0))
    accept(policy, prepare(policy, semantic_tokens=0))
    decision = policy.prepare(snapshot=normalize_snapshot({"goal": "new"}), semantic_tokens=9, **kwargs)
    assert decision.reason == reason


def test_initial_priority_and_call_over_token():
    policy = TaskStateReminderPolicy(call_interval=1, token_interval=1)
    assert prepare(policy, semantic_tokens=9, anchor_valid=False).reason == "initial"
    accept(policy, prepare(policy, semantic_tokens=0))
    accept(policy, prepare(policy, semantic_tokens=0))
    assert prepare(policy, semantic_tokens=9).reason == "call_interval"


def test_input_ids_are_captured_and_deduplicated_even_if_history_drops_them():
    policy = TaskStateReminderPolicy()
    ids = ["u1"]
    decision = prepare(policy, semantic_tokens=10, user_input_ids=ids, task_input_ids=["t1"])
    ids.append("u2")
    accept(policy, decision)
    assert prepare(policy, semantic_tokens=10, user_input_ids=["u1"], task_input_ids=["t1"]).reason == "unchanged"
    accept(policy, prepare(policy, semantic_tokens=10))
    assert prepare(policy, semantic_tokens=10, user_input_ids=["u1"]).reason == "unchanged"
    assert prepare(policy, semantic_tokens=10, user_input_ids=ids).reason == "user_message"
    assert prepare(policy, semantic_tokens=10, task_input_ids=["t2"]).reason == "task_message"


@pytest.mark.parametrize("tokens", [20, 200])
def test_explicit_history_reset_rebaselines(tokens):
    policy = TaskStateReminderPolicy()
    accept(policy, prepare(policy, semantic_tokens=100))
    decision = prepare(policy, semantic_tokens=tokens, anchor_valid=False)
    assert decision.reason == "history_reset"
    assert decision.new_semantic_tokens == 0
    accept(policy, decision)
    assert policy.state.semantic_tokens_at_snapshot == tokens
    assert prepare(policy, semantic_tokens=tokens + 1).new_semantic_tokens == 1


def test_stale_foreign_and_reset_decisions_cannot_commit():
    policy = TaskStateReminderPolicy()
    other = TaskStateReminderPolicy()
    first = prepare(policy, semantic_tokens=0)
    stale = prepare(policy, semantic_tokens=1)
    assert not accept(other, first)
    accept(policy, first)
    assert not accept(policy, stale)
    pending = prepare(policy, semantic_tokens=1)
    policy.reset()
    assert not accept(policy, pending)
    assert prepare(policy, semantic_tokens=0).reason == "initial"
    assert prepare(other, semantic_tokens=0).reason == "initial"


def test_normalized_snapshot_shared_by_fingerprint_and_render_and_is_detached():
    source = {"goal": "full goal " * 1000, "sampled_at": 1, "steps": ["a", "b"]}
    first = normalize_snapshot(source, ignored_fields={"sampled_at"}, renderer=lambda data: data["goal"])
    source["sampled_at"] = 2
    second = normalize_snapshot(dict(reversed(list(source.items()))), ignored_fields={"sampled_at"}, renderer=lambda data: data["goal"])
    assert first == second
    assert first.text == "full goal " * 1000
    source["steps"].reverse()
    assert normalize_snapshot(source, ignored_fields={"sampled_at"}).fingerprint != first.fingerprint
    assert first.data["steps"] == ["a", "b"]
    first.data["steps"].append("c")
    assert first.data["steps"] == ["a", "b"]


@pytest.mark.parametrize("value", [0, -1])
def test_threshold_validation(value):
    with pytest.raises(ValueError):
        TaskStateReminderPolicy(call_interval=value)
    with pytest.raises(ValueError):
        TaskStateReminderPolicy(token_interval=value)


def test_negative_token_validation():
    with pytest.raises(ValueError):
        prepare(TaskStateReminderPolicy(), semantic_tokens=-1)
    with pytest.raises(ValueError):
        prepare(TaskStateReminderPolicy(), semantic_tokens=0, snapshot_tokens=-1)


def test_token_decrease_clamps_delta_without_reset_or_prepare_mutation():
    policy = TaskStateReminderPolicy()
    accept(policy, prepare(policy, semantic_tokens=100))
    baseline = policy.state
    for tokens in (99, 20, 0):
        decision = prepare(policy, semantic_tokens=tokens)
        assert decision.reason == "unchanged"
        assert not decision.append_snapshot
        assert decision.new_semantic_tokens == 0
        assert policy.state is baseline
    assert accept(policy, decision)
    assert policy.state.semantic_tokens_at_snapshot == 100
    assert prepare(policy, semantic_tokens=101).new_semantic_tokens == 1
