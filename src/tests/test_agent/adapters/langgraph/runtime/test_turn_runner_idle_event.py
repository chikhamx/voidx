from __future__ import annotations

from types import SimpleNamespace

from voidx.agent.adapters.langgraph.runtime.turn_runner import TurnRunner


def test_turn_runner_starts_idle() -> None:
    runner = TurnRunner(SimpleNamespace())

    assert runner.idle_event.is_set()
