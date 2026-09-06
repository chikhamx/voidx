from langchain_core.messages import HumanMessage

from voidx.llm.cache_key import bind_prompt_cache_key, prompt_cache_key_for
from voidx.llm.providers.openai import ReasoningPreservingChatOpenAI


def test_prompt_cache_key_is_stable_and_scoped_to_session_model():
    first = prompt_cache_key_for("session-1", "jochen-gpt", "gpt-5.6-luna")

    assert first
    assert first == prompt_cache_key_for("session-1", "jochen-gpt", "gpt-5.6-luna")
    assert first != prompt_cache_key_for("session-2", "jochen-gpt", "gpt-5.6-luna")
    assert first != prompt_cache_key_for("session-1", "jochen-grok", "gpt-5.6-luna")
    assert first != prompt_cache_key_for("session-1", "jochen-gpt", "gpt-5.5")
    assert "session-1" not in first
    assert len(first) <= 64
    assert first != prompt_cache_key_for("SESSION-1", "jochen-gpt", "gpt-5.6-luna")


def test_prompt_cache_key_is_not_bound_without_a_session():
    assert prompt_cache_key_for(None, "jochen-gpt", "gpt-5.6-luna") is None
    assert prompt_cache_key_for("", "jochen-gpt", "gpt-5.6-luna") is None


def test_prompt_cache_key_is_sent_as_top_level_chat_completion_parameter():
    key = prompt_cache_key_for("session-1", "jochen-gpt", "gpt-5.6-luna")
    model = ReasoningPreservingChatOpenAI(
        api_key="test-key",
        model="gpt-5.6-luna",
        base_url="http://localhost/v1",
    )

    bound = bind_prompt_cache_key(model, key)
    payload = bound.bound._get_request_payload(
        [HumanMessage(content="hello")],
        **bound.kwargs,
    )

    assert payload["prompt_cache_key"] == key
    assert "extra_body" not in payload
