import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from voidx.config import ModelConfig
from voidx.llm.provider import create_chat_model, extract_thinking, resolve_protocol


PROVIDER_DEFAULTS = {
    "anthropic": ("anthropic", ChatAnthropic, "https://api.anthropic.com"),
    "deepseek": ("anthropic", ChatAnthropic, "https://api.deepseek.com/anthropic"),
    "openai": ("openai", ChatOpenAI, "https://api.openai.com/v1"),
    "openrouter": ("openai", ChatOpenAI, "https://openrouter.ai/api/v1"),
    "mimo": ("anthropic", ChatAnthropic, "https://api.xiaomimimo.com/anthropic"),
    "qwen": ("anthropic", ChatAnthropic, "https://dashscope.aliyuncs.com/apps/anthropic"),
    "zhipu": ("anthropic", ChatAnthropic, "https://open.bigmodel.cn/api/anthropic"),
    "kimi": ("anthropic", ChatAnthropic, "https://api.moonshot.cn/anthropic"),
    "doubao": ("openai", ChatOpenAI, "https://ark.cn-beijing.volces.com/api/v3"),
}


def test_resolve_protocol_defaults():
    for provider, (protocol, _, _) in PROVIDER_DEFAULTS.items():
        assert resolve_protocol(ModelConfig(provider=provider)) == protocol


def test_create_chat_model_uses_explicit_official_base_urls_for_core_providers():
    anthropic = create_chat_model(
        "test-key",
        ModelConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    openai = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-4o"),
    )

    assert isinstance(anthropic, ChatAnthropic)
    assert anthropic.anthropic_api_url == "https://api.anthropic.com"
    assert isinstance(openai, ChatOpenAI)
    assert str(openai.openai_api_base).rstrip("/") == "https://api.openai.com/v1"


def test_create_chat_model_uses_all_provider_default_base_urls():
    for provider, (_, expected_type, expected_base_url) in PROVIDER_DEFAULTS.items():
        model = create_chat_model(
            "test-key",
            ModelConfig(provider=provider, model="test-model"),
        )

        assert isinstance(model, expected_type)
        if isinstance(model, ChatAnthropic):
            assert model.anthropic_api_url == expected_base_url
        else:
            assert str(model.openai_api_base).rstrip("/") == expected_base_url


def test_extract_thinking_from_anthropic_chunks():
    chunk = AIMessageChunk(content=[
        {"type": "thinking", "thinking": "anthropic thought"},
        {"type": "redacted_thinking", "data": "redacted"},
    ])

    assert extract_thinking(chunk, "anthropic") == "anthropic thoughtredacted"


def test_extract_thinking_from_openai_compatible_chunks():
    assert extract_thinking(
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "compat"}),
        "openai",
    ) == "compat"
    assert extract_thinking(
        AIMessageChunk(content=[{"type": "reasoning", "reasoning": "responses"}]),
        "openai",
    ) == "responses"
    assert extract_thinking(
        AIMessageChunk(content=[
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "summary"},
                    {"type": "summary_text", "text": " text"},
                ],
            }
        ]),
        "openai",
    ) == "summary text"
    assert extract_thinking(
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning": {
                    "summary": [{"type": "summary_text", "text": "kwarg summary"}]
                }
            },
        ),
        "openai",
    ) == "kwarg summary"
