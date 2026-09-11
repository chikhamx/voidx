"""Run-local reminder decisions; callers own history, token estimation and delivery."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Literal


DEFAULT_CALL_INTERVAL = 5
DEFAULT_TOKEN_INTERVAL = 8192
ReminderReason = Literal[
    "initial", "history_reset", "user_message", "task_message",
    "state_changed", "call_interval", "token_interval", "unchanged",
]


@dataclass(frozen=True)
class NormalizedSnapshot:
    canonical_json: str
    fingerprint: str
    text: str

    @property
    def data(self) -> Any:
        """Return a detached view so renderers cannot mutate the stored snapshot."""
        return json.loads(self.canonical_json)


def normalize_snapshot(
    data: Any,
    *,
    ignored_fields: Iterable[str] = (),
    renderer: Callable[[Any], str] | None = None,
) -> NormalizedSnapshot:
    """Normalize JSON-like data, preserving sequence order and complete values.

    Callers extract decision-relevant fields and explicitly name volatile fields.
    Mapping order is immaterial; semantic sequence order is never sorted.
    Renderers must be deterministic functions of the supplied normalized data.
    """
    ignored = frozenset(ignored_fields)

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise TypeError("snapshot mapping keys must be strings")
            return {key: clean(item) for key, item in value.items() if key not in ignored}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return value

    canonical = json.dumps(
        clean(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    text = renderer(json.loads(canonical)) if renderer else canonical
    if not isinstance(text, str):
        raise TypeError("snapshot renderer must return text")
    return NormalizedSnapshot(canonical, hashlib.sha256(canonical.encode()).hexdigest(), text)


@dataclass(frozen=True)
class ReminderPolicyState:
    snapshot: NormalizedSnapshot | None = None
    calls_since_snapshot: int = 0
    semantic_tokens_at_snapshot: int = 0
    user_input_ids: frozenset[str] = frozenset()
    task_input_ids: frozenset[str] = frozenset()
    snapshot_tokens: int = 0


@dataclass(frozen=True)
class PreparedReminder:
    reason: ReminderReason
    snapshot: NormalizedSnapshot
    baseline: ReminderPolicyState
    semantic_tokens: int
    snapshot_tokens: int
    new_semantic_tokens: int
    user_input_ids: frozenset[str]
    task_input_ids: frozenset[str]
    anchor_valid: bool
    _context: object = field(repr=False, compare=False)

    @property
    def append_snapshot(self) -> bool:
        return self.reason != "unchanged"


class TaskStateReminderPolicy:
    """One instance per conversation/run; prepare is read-only, commit is explicit.

    Do not commit failed, aborted or unaccepted responses. Prepare a fresh decision
    after another logical request commits. Reset on model/profile/context changes.
    """

    def __init__(
        self, *, call_interval: int = DEFAULT_CALL_INTERVAL,
        token_interval: int = DEFAULT_TOKEN_INTERVAL,
    ) -> None:
        if call_interval <= 0 or token_interval <= 0:
            raise ValueError("reminder thresholds must be positive")
        self.call_interval = call_interval
        self.token_interval = token_interval
        self.reset()

    @property
    def state(self) -> ReminderPolicyState:
        return self._state

    def reset(self) -> None:
        self._context = object()
        self._state = ReminderPolicyState()

    def prepare(
        self, *, snapshot: NormalizedSnapshot, semantic_tokens: int,
        snapshot_tokens: int = 0, user_input_ids: Iterable[str] = (),
        task_input_ids: Iterable[str] = (), anchor_valid: bool = True,
    ) -> PreparedReminder:
        """Tokens exclude system/tools definitions and every task-state snapshot.

        IDs identify accepted real input events, not message roles or text.
        anchor_valid is false only for an explicit caller-owned history reset.
        Content trimming and token decreases do not imply a reset.
        snapshot_tokens is diagnostic only and never contributes to the delta.
        """
        if semantic_tokens < 0 or snapshot_tokens < 0:
            raise ValueError("token estimates must be nonnegative")
        baseline = self._state
        users, tasks = frozenset(user_input_ids), frozenset(task_input_ids)
        history_reset = not anchor_valid
        delta = 0 if baseline.snapshot is None or history_reset else max(0, semantic_tokens - baseline.semantic_tokens_at_snapshot)
        reason: ReminderReason
        if baseline.snapshot is None:
            reason = "initial"
        elif history_reset:
            reason = "history_reset"
        elif users - baseline.user_input_ids:
            reason = "user_message"
        elif tasks - baseline.task_input_ids:
            reason = "task_message"
        elif snapshot.fingerprint != baseline.snapshot.fingerprint:
            reason = "state_changed"
        elif baseline.calls_since_snapshot >= self.call_interval:
            reason = "call_interval"
        elif delta >= self.token_interval:
            reason = "token_interval"
        else:
            reason = "unchanged"
        return PreparedReminder(
            reason, snapshot, baseline, semantic_tokens, snapshot_tokens, delta,
            users, tasks, anchor_valid, self._context,
        )

    def commit(
        self, decision: PreparedReminder, *, successful: bool, accepted: bool,
    ) -> bool:
        """Return whether committed; duplicates, stale and foreign decisions are no-ops."""
        if (
            not successful or not accepted
            or decision._context is not self._context
            or decision.baseline is not self._state
        ):
            return False
        baseline = decision.baseline
        if decision.append_snapshot:
            self._state = ReminderPolicyState(
                snapshot=decision.snapshot,
                semantic_tokens_at_snapshot=decision.semantic_tokens,
                user_input_ids=baseline.user_input_ids | decision.user_input_ids,
                task_input_ids=baseline.task_input_ids | decision.task_input_ids,
                snapshot_tokens=decision.snapshot_tokens,
            )
        else:
            self._state = ReminderPolicyState(
                snapshot=baseline.snapshot,
                calls_since_snapshot=baseline.calls_since_snapshot + 1,
                semantic_tokens_at_snapshot=baseline.semantic_tokens_at_snapshot,
                user_input_ids=baseline.user_input_ids,
                task_input_ids=baseline.task_input_ids,
                snapshot_tokens=baseline.snapshot_tokens,
            )
        return True
