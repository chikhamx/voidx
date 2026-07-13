from __future__ import annotations

from types import SimpleNamespace

from voidx.agent.graph.turn_runner import GraphTurnRunner


def test_turn_runner_starts_idle() -> None:
    runner = GraphTurnRunner(SimpleNamespace())

    assert runner.idle_event.is_set()
