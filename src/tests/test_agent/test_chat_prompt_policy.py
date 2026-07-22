"""Prompt policy contracts for runtime profiles."""

from __future__ import annotations

from voidx.agent.application.chat_service import CHAT_PROFILE
from voidx.agent.domain.prompt_policy import (
    ChatPromptPolicy,
    CodingPromptPolicy,
    PromptPolicy,
)
from voidx.agent.prompts import CHAT_PROFILE_SPEC
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.runtime.contracts import TurnRequest
from voidx.agent.domain.thread import AgentThread


def test_coding_prompt_policy_returns_none_for_all_overrides():
    policy = CodingPromptPolicy()

    assert policy.persona_prompt is None
    assert policy.workflow_runtime is None
    assert policy.task_state_section is None
    assert policy.profile_directive is None


def test_chat_prompt_policy_suppresses_coding_sections():
    policy = ChatPromptPolicy()

    assert policy.persona_prompt == ""
    assert policy.workflow_runtime == ""
    assert policy.task_state_section == ""
    assert policy.profile_directive is not None
    assert "chat" in policy.profile_directive.lower()


def test_chat_prompt_policy_selects_chat_base_system_spec():
    policy = ChatPromptPolicy()

    assert policy.base_system_spec is CHAT_PROFILE_SPEC


def test_coding_prompt_policy_keeps_default_base_system_spec():
    policy = CodingPromptPolicy()

    assert policy.base_system_spec is None


def test_chat_prompt_policy_directive_states_bound_tools_and_restrictions():
    policy = ChatPromptPolicy()
    directive = policy.profile_directive

    assert "read-only" in directive.lower() or "read only" in directive.lower()
    assert "shell" in directive.lower() or "write" in directive.lower()
    assert "mcp" in directive.lower()


def test_chat_profile_carries_chat_prompt_policy():
    assert CHAT_PROFILE.prompt_policy is not None
    assert isinstance(CHAT_PROFILE.prompt_policy, ChatPromptPolicy)


def test_coding_profile_default_has_no_prompt_policy():
    profile = RuntimeProfile(profile_id="coding", revision=1, name="Coding")

    assert profile.prompt_policy is None


def test_turn_request_default_coding_profile_has_no_prompt_policy():
    request = TurnRequest(
        thread=AgentThread(thread_id="t", session_id="s"),
        user_text="hi",
    )

    assert request.profile.prompt_policy is None


def test_prompt_policy_is_protocol():
    import typing

    assert typing.get_type_hints(PromptPolicy) or hasattr(PromptPolicy, "persona_prompt")
