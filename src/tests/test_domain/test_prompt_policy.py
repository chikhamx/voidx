"""PromptPolicy three-method interface contracts."""

from __future__ import annotations

from voidx.agent.application.prompts import CHAT_PROFILE_SPEC
from voidx.agent.application.runtime_context import ContextSection
from voidx.agent.domain.goal import (
    GOAL_EVALUATOR_DIRECTIVE,
    GOAL_IDLE_DIRECTIVE,
    GOAL_INTAKE_DIRECTIVE,
    GOAL_PROFILE,
)
from voidx.agent.domain.loop import (
    LOOP_IDLE_DIRECTIVE,
    LOOP_PROFILE,
)
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import (
    ChatPromptPolicy,
    CodingPromptPolicy,
    GoalPromptPolicy,
    LoopPromptPolicy,
)
from voidx.agent.domain.turn_context import TurnExecutionContext


def _turn_context(*, protocol: str = "turn", goal_phase: str = "work", loop_phase: str = "work", system_prompt: str = "") -> TurnExecutionContext:
    profile = RuntimeProfile(
        profile_id=protocol,
        revision=1,
        name=protocol.capitalize(),
        protocol=protocol,
        system_prompt=system_prompt,
    )
    return TurnExecutionContext(
        thread_id="t",
        session_id="s",
        runtime_profile=profile,
        goal_phase=goal_phase,
        loop_phase=loop_phase,
    )


class TestCodingPromptPolicy:
    def test_base_system_spec_is_none(self):
        assert CodingPromptPolicy().base_system_spec() is None

    def test_profile_sections_empty(self):
        assert CodingPromptPolicy().profile_sections(None) == []

    def test_suppress_sections_empty(self):
        assert CodingPromptPolicy().suppress_sections() == set()


class TestChatPromptPolicy:
    def test_base_system_spec_is_chat_spec(self):
        assert ChatPromptPolicy().base_system_spec() is CHAT_PROFILE_SPEC

    def test_profile_sections_returns_directive(self):
        sections = ChatPromptPolicy().profile_sections(None)
        assert len(sections) == 1
        assert sections[0].name == "Profile Directive"
        assert "chat" in sections[0].content.lower()

    def test_suppress_sections_covers_coding_only_sections(self):
        suppress = ChatPromptPolicy().suppress_sections()
        assert "Persona" in suppress
        assert "Workflow Runtime" in suppress
        assert "Current Task State" in suppress


class TestGoalPromptPolicy:
    def test_base_system_spec_is_none(self):
        assert GoalPromptPolicy().base_system_spec() is None

    def test_suppress_sections_empty(self):
        assert GoalPromptPolicy().suppress_sections() == set()

    def test_profile_sections_idle_phase(self):
        ctx = _turn_context(protocol="goal", goal_phase="idle")
        sections = GoalPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        assert sections[0].content == GOAL_IDLE_DIRECTIVE

    def test_profile_sections_intake_phase(self):
        ctx = _turn_context(protocol="goal", goal_phase="intake")
        sections = GoalPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        assert sections[0].content == GOAL_INTAKE_DIRECTIVE

    def test_profile_sections_evaluator_phase(self):
        ctx = _turn_context(protocol="goal", goal_phase="evaluator")
        sections = GoalPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        assert sections[0].content == GOAL_EVALUATOR_DIRECTIVE

    def test_profile_sections_work_phase_empty(self):
        ctx = _turn_context(protocol="goal", goal_phase="work")
        assert GoalPromptPolicy().profile_sections(ctx) == []

    def test_profile_sections_unknown_phase_empty(self):
        ctx = _turn_context(protocol="goal", goal_phase="unknown")
        assert GoalPromptPolicy().profile_sections(ctx) == []

    def test_profile_sections_none_context_empty(self):
        assert GoalPromptPolicy().profile_sections(None) == []


class TestLoopPromptPolicy:
    def test_base_system_spec_is_none(self):
        assert LoopPromptPolicy().base_system_spec() is None

    def test_suppress_sections_empty(self):
        assert LoopPromptPolicy().suppress_sections() == set()

    def test_profile_sections_idle_phase(self):
        ctx = _turn_context(protocol="loop", loop_phase="idle")
        sections = LoopPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        assert sections[0].content == LOOP_IDLE_DIRECTIVE

    def test_profile_sections_includes_loop_system_prompt(self):
        ctx = _turn_context(protocol="loop", loop_phase="work", system_prompt="## Loop Goal\nDo thing.")
        sections = LoopPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        assert "## Loop Goal" in sections[0].content

    def test_profile_sections_idle_includes_system_prompt(self):
        ctx = _turn_context(protocol="loop", loop_phase="idle", system_prompt="## Loop Goal\nDo thing.")
        sections = LoopPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        content = sections[0].content
        assert content.index(LOOP_IDLE_DIRECTIVE) < content.index("## Loop Goal")

    def test_profile_sections_idle_empty_system_prompt_returns_directive_only(self):
        ctx = _turn_context(protocol="loop", loop_phase="idle", system_prompt="")
        sections = LoopPromptPolicy().profile_sections(ctx)
        assert len(sections) == 1
        assert sections[0].content == LOOP_IDLE_DIRECTIVE

    def test_profile_sections_work_phase_no_system_prompt_empty(self):
        ctx = _turn_context(protocol="loop", loop_phase="work")
        assert LoopPromptPolicy().profile_sections(ctx) == []

    def test_profile_sections_none_context_empty(self):
        assert LoopPromptPolicy().profile_sections(None) == []


class TestProfileWiring:
    def test_goal_profile_carries_goal_prompt_policy(self):
        assert isinstance(GOAL_PROFILE.prompt_policy, GoalPromptPolicy)

    def test_loop_profile_carries_loop_prompt_policy(self):
        assert isinstance(LOOP_PROFILE.prompt_policy, LoopPromptPolicy)

    def test_loop_profile_for_spec_preserves_prompt_policy(self):
        from voidx.agent.domain.loop import LoopSpec, loop_profile_for_spec

        spec = LoopSpec(prompt="test", interval_seconds=60)
        profile = loop_profile_for_spec(spec)
        assert isinstance(profile.prompt_policy, LoopPromptPolicy)
