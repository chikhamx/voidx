"""Prompt policy contracts for runtime profiles."""

from __future__ import annotations

from voidx.agent.application.chat_service import CHAT_PROFILE
from voidx.agent.domain.prompt_policy import (
    ChatPromptPolicy,
    CodingPromptPolicy,
    PromptPolicy,
)
from voidx.agent.application.prompts import CHAT_PROFILE_SPEC
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.application.runtime.contracts import TurnRequest
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext


def test_coding_prompt_policy_returns_empty_sections():
    policy = CodingPromptPolicy()

    assert policy.base_system_spec() is None
    assert policy.profile_sections(None) == []
    assert policy.suppress_sections() == set()


def test_chat_prompt_policy_suppresses_coding_sections():
    policy = ChatPromptPolicy()

    suppress = policy.suppress_sections()
    assert "Persona" in suppress
    assert "Workflow Runtime" in suppress
    assert "Current Task State" in suppress


def test_chat_prompt_policy_selects_chat_base_system_spec():
    policy = ChatPromptPolicy()

    assert policy.base_system_spec() is CHAT_PROFILE_SPEC


def test_coding_prompt_policy_keeps_default_base_system_spec():
    policy = CodingPromptPolicy()

    assert policy.base_system_spec() is None


def test_chat_prompt_policy_has_no_profile_directive():
    policy = ChatPromptPolicy()
    assert policy.profile_sections(None) == []


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
        context=TurnExecutionContext(thread_id="t", session_id="s"),
    )

    assert request.context.runtime_profile.prompt_policy is None


def test_prompt_policy_is_protocol():
    from typing import Protocol

    assert isinstance(PromptPolicy, type(Protocol))
