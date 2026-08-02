"""Goal intake service — first-message to confirmed GoalSpec."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.application.goal_intake import GoalIntakeError, GoalIntakeService
from voidx.agent.domain.goal import GoalSpec, GoalToolView
from voidx.agent.domain.thread import AgentThread


class FakeRuntime:
    def __init__(self, spec: dict | None = None, summary: str = ""):
        self.spec = spec
        self.summary = summary
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        controller = getattr(request.context, "goal_intake_controller", None)
        if self.spec is not None and controller is not None:
            await controller.submit_init(GoalSpec(**self.spec))
        return SimpleNamespace(final_assistant_summary=self.summary)


class FakeGoalService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True)


@pytest.mark.asyncio
async def test_intake_runs_restricted_turn_and_starts_goal_from_init_tool() -> None:
    runtime = FakeRuntime(
        {
            "objective": "fix flaky tests",
            "acceptance_condition": "suite green",
            "achievement_method": "",
        },
        summary="this final text should not be parsed",
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
    assert request.context.goal_intake_controller is not None
    assert request.context.tool_policy.allows("clarify")
    assert request.context.tool_policy.allows("goal")
    assert not request.context.tool_policy.allows("bash")
    assert not request.context.tool_policy.allows("write")
    assert request.display_text == "make the tests reliable"
    assert 'op="init"' in request.user_text
    assert "Intake workflow" in request.user_text
    assert "NEVER perform the task itself" in request.user_text
    assert "objective" in request.user_text
    assert "acceptance_condition" in request.user_text
    assert "achievement_method" in request.user_text
    assert "schedule" in request.user_text
    assert "max_attempts" in request.user_text
    assert "Required output" not in request.user_text


@pytest.mark.asyncio
async def test_intake_raises_when_goal_init_not_submitted() -> None:
    runtime = FakeRuntime(summary='{"objective": "ignored", "acceptance_condition": "ignored"}')
    service = GoalIntakeService(runtime, FakeGoalService())

    with pytest.raises(GoalIntakeError) as exc_info:
        await service.run("vague", "host-1")

    assert 'goal(op="init")' in str(exc_info.value)


@pytest.mark.asyncio
async def test_intake_rejects_incomplete_init_spec() -> None:
    runtime = FakeRuntime({"objective": "only objective", "acceptance_condition": "", "achievement_method": ""})
    service = GoalIntakeService(runtime, FakeGoalService())

    with pytest.raises(ValueError):
        await service.run("vague", "host-1")


def test_goal_tool_view_intake_phase_binds_clarify_and_goal_only() -> None:
    view = GoalToolView.default(phase="intake").bind(
        {"read", "clarify", "goal", "bash", "write", "websearch", "mcp"}
    )

    assert view.allows("clarify")
    assert view.allows("goal")
    assert view.allows("read")
    assert not view.allows("websearch")
    assert not view.allows("mcp")
    assert not view.allows("bash")
    assert not view.allows("write")
