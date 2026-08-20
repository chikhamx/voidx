from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeUi:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, value: str) -> None:
        self.lines.append(str(value))

    def error(self, value: str) -> None:
        self.lines.append(f"ERROR: {value}")


@pytest.mark.asyncio
async def test_agents_list_discovers_fresh_profiles_each_time(monkeypatch) -> None:
    from voidx.presentation.slash.commands.agents import AgentsCommandsMixin

    calls: list[str] = []
    ui = FakeUi()

    def discover(workspace: str):
        calls.append(workspace)
        display = "First" if len(calls) == 1 else "Second"
        return [
            SimpleNamespace(
                name="custom-review",
                display_name=display,
                source="project",
                available=True,
                diagnostics=(),
            )
        ]

    monkeypatch.setattr("voidx.presentation.slash.commands.agents.list_agent_profiles", discover)

    class Handler(AgentsCommandsMixin):
        mode_port = SimpleNamespace(workspace="/workspace", ui=ui)

    handler = Handler()
    await handler._agents("list")
    await handler._agents("list")

    assert calls == ["/workspace", "/workspace"]
    assert any("First" in line for line in ui.lines)
    assert any("Second" in line for line in ui.lines)


@pytest.mark.asyncio
async def test_agents_use_resolves_and_switches_with_snapshot(monkeypatch) -> None:
    from voidx.presentation.slash.commands.agents import AgentsCommandsMixin

    resolved = SimpleNamespace(snapshot=SimpleNamespace(profile_id="custom-review"))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "voidx.presentation.slash.commands.agents.resolve_agent_profile",
        lambda workspace, name: resolved,
    )

    class Handler(AgentsCommandsMixin):
        mode_port = SimpleNamespace(workspace="/workspace", ui=FakeUi())

        async def _switch_profile(self, profile: str, *, resolved_profile=None) -> None:
            assert resolved_profile is resolved
            calls.append((profile, resolved_profile.snapshot.profile_id))

    await Handler()._agents("use custom-review")

    assert calls == [("custom-review", "custom-review")]


@pytest.mark.asyncio
async def test_agents_use_reports_unavailable_diagnostics_without_switch(monkeypatch) -> None:
    from voidx.agent.application.agent_profile_loader import ProfileLoadError
    from voidx.agent.domain.agent_profile import ProfileDiagnostic
    from voidx.presentation.slash.commands.agents import AgentsCommandsMixin

    ui = FakeUi()
    diagnostic = ProfileDiagnostic(
        path="", code="invalid_profile", message="workflow is invalid"
    )

    def unavailable(workspace: str, name: str):
        raise ProfileLoadError([diagnostic])

    monkeypatch.setattr(
        "voidx.presentation.slash.commands.agents.resolve_agent_profile", unavailable
    )

    class Handler(AgentsCommandsMixin):
        mode_port = SimpleNamespace(workspace="/workspace", ui=ui)

        async def _switch_profile(self, profile: str, *, resolved_profile=None) -> None:
            raise AssertionError("unavailable profile must not switch")

    await Handler()._agents("use broken")

    assert any("workflow is invalid" in line for line in ui.lines)
