"""Goal intake service — first-message to confirmed GoalSpec."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.application.goal_intake import (
    GoalIntakeError,
    GoalIntakeService,
    _extract_json,
)
from voidx.agent.domain.goal import GoalToolView
from voidx.agent.domain.thread import AgentThread


class FakeRuntime:
    def __init__(self, summary: str = ""):
        self.summary = summary
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        return SimpleNamespace(final_assistant_summary=self.summary)


class FakeGoalService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True)


def test_extract_json_parses_plain_and_fenced() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert _extract_json("explanation ```\n{\"a\": 1}\n```") == {"a": 1}
    assert _extract_json("not json") is None
    assert _extract_json("") is None


@pytest.mark.asyncio
async def test_intake_runs_restricted_turn_and_starts_goal() -> None:
    runtime = FakeRuntime(
        '{"objective": "fix flaky tests", "acceptance_condition": "suite green", '
        '"achievement_method": ""}'
    )
    service = GoalIntakeService(runtime, goal_service := FakeGoalService())

    status = await service.run("make the tests reliable", "host-1")

    assert status.active is True
    assert len(goal_service.started) == 1
    parent, spec = goal_service.started[0]
    assert parent == "host-1"
    assert spec.objective == "fix flaky tests"
    assert spec.acceptance_condition == "suite green"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert isinstance(request.thread, AgentThread)
    assert request.thread.thread_id == "goal-intake:host-1"
    assert request.context.runtime_profile.profile_id == "goal"
    assert request.context.goal_phase == "intake"
    assert request.context.tool_policy.allows("clarify")
    assert not request.context.tool_policy.allows("bash")
    assert not request.context.tool_policy.allows("write")


@pytest.mark.asyncio
async def test_intake_non_json_raises() -> None:
    runtime = FakeRuntime("I could not determine a goal.")
    service = GoalIntakeService(runtime, FakeGoalService())

    with pytest.raises(GoalIntakeError):
        await service.run("vague", "host-1")


@pytest.mark.asyncio
async def test_intake_incomplete_spec_raises() -> None:
    runtime = FakeRuntime('{"objective": "only objective", "acceptance_condition": "", "achievement_method": ""}')
    service = GoalIntakeService(runtime, FakeGoalService())

    with pytest.raises(GoalIntakeError):
        await service.run("vague", "host-1")


def test_goal_tool_view_intake_phase_binds_clarify_only() -> None:
    view = GoalToolView.default(phase="intake").bind(
        {"read", "clarify", "bash", "write", "websearch", "mcp"}
    )

    assert view.allows("clarify")
    assert view.allows("read")
    assert view.allows("websearch")
    assert view.allows("mcp")
    assert not view.allows("bash")
    assert not view.allows("write")
