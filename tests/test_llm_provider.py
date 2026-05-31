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
    "mimo-token-plan": ("anthropic", ChatAnthropic, "https://token-plan-cn.xiaomimimo.com/anthropic"),
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


def test_reasoning_kwargs_are_provider_specific():
    sonnet = create_chat_model(
        "test-key",
        ModelConfig(provider="anthropic", model="claude-sonnet-4-6", reasoning_effort="medium"),
    )
    assert sonnet.thinking == {"type": "enabled", "budget_tokens": 4096}
    assert sonnet.effort is None

    opus = create_chat_model(
        "test-key",
        ModelConfig(provider="anthropic", model="claude-opus-4-7", reasoning_effort="xhigh"),
    )
    assert opus.thinking == {"type": "adaptive"}
    assert opus.effort == "xhigh"

    mimo = create_chat_model(
        "test-key",
        ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="medium"),
    )
    assert mimo.thinking == {"type": "enabled"}

    mimo_off = create_chat_model(
        "test-key",
        ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="off"),
    )
    assert mimo_off.thinking == {"type": "disabled"}

    for provider in ("qwen", "zhipu", "kimi"):
        model = create_chat_model(
            "test-key",
            ModelConfig(provider=provider, model="test-model", reasoning_effort="medium"),
        )
        assert model.thinking is None
        assert model.effort is None

    # deepseek: binary thinking (enabled/disabled only, no budget_tokens)
    deepseek_medium = create_chat_model(
        "test-key",
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="medium"),
    )
    assert deepseek_medium.thinking == {"type": "enabled"}

    deepseek_off = create_chat_model(
        "test-key",
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="off"),
    )
    assert deepseek_off.thinking == {"type": "disabled"}


def test_openai_compatible_reasoning_is_model_gated():
    gpt5 = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-5.4-mini", reasoning_effort="medium"),
    )
    assert gpt5.reasoning_effort == "medium"

    gpt4o = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-4o", reasoning_effort="medium"),
    )
    assert gpt4o.reasoning_effort is None

    gpt5_off = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-5.4-mini", reasoning_effort="off"),
    )
    assert gpt5_off.reasoning_effort == "none"

    # o1/o3/o4: off → low (无法完全关闭, 最小化推理)
    o1_off = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="o1-mini", reasoning_effort="off"),
    )
    assert o1_off.reasoning_effort == "low"

    o3_off = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="o3-mini", reasoning_effort="off"),
    )
    assert o3_off.reasoning_effort == "low"

    o4_off = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="o4-mini", reasoning_effort="off"),
    )
    assert o4_off.reasoning_effort == "low"

    # o1/o3/o4: 正常值透传
    o1_medium = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="o1-mini", reasoning_effort="medium"),
    )
    assert o1_medium.reasoning_effort == "medium"

    openrouter = create_chat_model(
        "test-key",
        ModelConfig(provider="openrouter", model="any/reasoning-model", reasoning_effort="off"),
    )
    assert openrouter.reasoning_effort is None
    assert openrouter.extra_body == {"reasoning": {"effort": "none"}}

    doubao_thinking = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="medium"),
    )
    assert doubao_thinking.reasoning_effort is None
    assert doubao_thinking.extra_body == {"thinking": {"type": "enabled"}}

    doubao_off = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="off"),
    )
    assert doubao_off.extra_body == {"thinking": {"type": "disabled"}}

    doubao_plain = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-lite", reasoning_effort="medium"),
    )
    assert doubao_plain.reasoning_effort is None
    assert doubao_plain.extra_body is None


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
