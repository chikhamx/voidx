import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from voidx.config import ModelConfig
from voidx.llm.provider import DeepSeekChatOpenAI, create_chat_model, extract_thinking, resolve_protocol


PROVIDER_DEFAULTS = {
    "anthropic": ("anthropic", ChatAnthropic, "https://api.anthropic.com"),
    "deepseek": ("deepseek", DeepSeekChatOpenAI, "https://api.deepseek.com/v1"),
    "openai": ("openai", ChatOpenAI, "https://api.openai.com/v1"),
    "openrouter": ("openai", ChatOpenAI, "https://openrouter.ai/api/v1"),
    "mimo": ("deepseek", DeepSeekChatOpenAI, "https://api.xiaomimimo.com/v1"),
    "mimo-token-plan": ("deepseek", DeepSeekChatOpenAI, "https://token-plan-cn.xiaomimimo.com/v1"),
    "qwen": ("deepseek", DeepSeekChatOpenAI, "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "zhipu": ("deepseek", DeepSeekChatOpenAI, "https://open.bigmodel.cn/api/paas/v4"),
    "kimi": ("deepseek", DeepSeekChatOpenAI, "https://api.moonshot.cn/v1"),
    "doubao": ("deepseek", DeepSeekChatOpenAI, "https://ark.cn-beijing.volces.com/api/v3"),
    "minimax": ("deepseek", DeepSeekChatOpenAI, "https://api.minimax.io/v1"),
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
    assert isinstance(mimo, DeepSeekChatOpenAI)
    assert mimo.extra_body == {"thinking": {"type": "enabled"}}

    mimo_off = create_chat_model(
        "test-key",
        ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="off"),
    )
    assert isinstance(mimo_off, DeepSeekChatOpenAI)
    assert mimo_off.extra_body == {"thinking": {"type": "disabled"}}

    for provider in ("kimi",):
        model = create_chat_model(
            "test-key",
            ModelConfig(provider=provider, model="test-model", reasoning_effort="medium"),
        )
        assert isinstance(model, DeepSeekChatOpenAI)
        assert model.extra_body == {"thinking": {"type": "enabled"}}

    # deepseek: thinking toggle + reasoning_effort
    deepseek_medium = create_chat_model(
        "test-key",
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="medium"),
    )
    assert isinstance(deepseek_medium, DeepSeekChatOpenAI)
    assert deepseek_medium.extra_body == {"thinking": {"type": "enabled"}}
    assert deepseek_medium.reasoning_effort == "high"

    deepseek_off = create_chat_model(
        "test-key",
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="off"),
    )
    assert isinstance(deepseek_off, DeepSeekChatOpenAI)
    assert deepseek_off.extra_body == {"thinking": {"type": "disabled"}}
    assert deepseek_off.reasoning_effort is None

    deepseek_xhigh = create_chat_model(
        "test-key",
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="xhigh"),
    )
    assert isinstance(deepseek_xhigh, DeepSeekChatOpenAI)
    assert deepseek_xhigh.extra_body == {"thinking": {"type": "enabled"}}
    assert deepseek_xhigh.reasoning_effort == "max"


def test_openai_compatible_reasoning_uses_nested_format():
    """OpenAI reasoning uses extra_body={reasoning: {effort: ...}} (nested format).

    The flat reasoning_effort parameter is rejected by gpt-5.x on /v1/chat/completions.
    All OpenAI-compatible reasoning models now use the nested format via extra_body.
    """
    # gpt-5.x: effort via extra_body
    gpt5 = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-5.4-mini", reasoning_effort="medium"),
    )
    assert gpt5.reasoning_effort is None
    assert gpt5.extra_body == {"reasoning": {"effort": "medium"}}

    # gpt-4o: no reasoning support, no params injected
    gpt4o = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-4o", reasoning_effort="medium"),
    )
    assert gpt4o.reasoning_effort is None
    assert gpt4o.extra_body is None

    # gpt-5.x off → effort "none" via extra_body
    gpt5_off = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-5.4-mini", reasoning_effort="off"),
    )
    assert gpt5_off.extra_body == {"reasoning": {"effort": "none"}}

    # o3: off → "low" (cannot fully disable)
    o3_off = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="o3", reasoning_effort="off"),
    )
    assert o3_off.extra_body == {"reasoning": {"effort": "low"}}

    # o4-mini: high effort
    o4 = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="o4-mini", reasoning_effort="high"),
    )
    assert o4.extra_body == {"reasoning": {"effort": "high"}}

    # gpt-5.x xhigh
    gpt5_xhigh = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-5.4-mini", reasoning_effort="xhigh"),
    )
    assert gpt5_xhigh.reasoning_effort is None
    assert gpt5_xhigh.extra_body == {"reasoning": {"effort": "xhigh"}}

    # openrouter: already uses extra_body nested format
    openrouter = create_chat_model(
        "test-key",
        ModelConfig(provider="openrouter", model="any/reasoning-model", reasoning_effort="off"),
    )
    assert openrouter.reasoning_effort is None
    assert openrouter.extra_body == {"reasoning": {"effort": "none"}}


def test_custom_provider_reasoning_not_auto_injected():
    """Custom providers with openai protocol do NOT get reasoning params
    auto-injected — third-party relays may not support them."""
    custom = create_chat_model(
        "test-key",
        ModelConfig(provider="my-relay", model="gpt-5.5", reasoning_effort="high", protocol="openai"),
    )
    assert custom.reasoning_effort is None
    assert custom.extra_body is None

    # Custom provider with non-reasoning model: also no params
    custom_plain = create_chat_model(
        "test-key",
        ModelConfig(provider="my-relay", model="gpt-4o", reasoning_effort="medium", protocol="openai"),
    )
    assert custom_plain.reasoning_effort is None
    assert custom_plain.extra_body is None

    # Custom provider with reasoning off: also no params
    custom_off = create_chat_model(
        "test-key",
        ModelConfig(provider="my-relay", model="gpt-5.5-mini", reasoning_effort="off", protocol="openai"),
    )
    assert custom_off.reasoning_effort is None
    assert custom_off.extra_body is None


def test_custom_provider_strips_stainless_headers():
    """Third-party relays with openai protocol get x-stainless-* headers cleared."""
    custom = create_chat_model(
        "test-key",
        ModelConfig(provider="my-relay", model="gpt-4o", protocol="openai"),
    )
    assert custom.default_headers is not None
    assert custom.default_headers.get("x-stainless-lang") == ""
    assert custom.default_headers.get("User-Agent") == "voidx/1.0"

    # Official openai provider keeps default headers
    official = create_chat_model(
        "test-key",
        ModelConfig(provider="openai", model="gpt-4o", protocol="openai"),
    )
    assert official.default_headers is None or "x-stainless-lang" not in (official.default_headers or {})

    # Official openrouter provider keeps default headers
    openrouter = create_chat_model(
        "test-key",
        ModelConfig(provider="openrouter", model="gpt-4o", protocol="openai"),
    )
    assert openrouter.default_headers is None or "x-stainless-lang" not in (openrouter.default_headers or {})

    # DeepSeek protocol does NOT strip headers
    ds_custom = create_chat_model(
        "test-key",
        ModelConfig(provider="my-relay", model="deepseek-r1", protocol="deepseek"),
    )
    assert ds_custom.default_headers is None

    # Anthropic protocol does NOT strip headers
    anthropic_custom = create_chat_model(
        "test-key",
        ModelConfig(provider="my-relay", model="claude-sonnet-4-6", protocol="anthropic"),
    )
    assert anthropic_custom.default_headers is None


def test_typex_reasoning_uses_zhipu_thinking_format():
    """typex hosts Zhipu GLM models which use thinking: {type: ...} format."""
    typex = create_chat_model(
        "test-key",
        ModelConfig(provider="typex", model="zai-org/GLM-5-FP8", reasoning_effort="high"),
    )
    assert isinstance(typex, DeepSeekChatOpenAI)
    assert typex.extra_body == {"thinking": {"type": "enabled"}}

    typex_off = create_chat_model(
        "test-key",
        ModelConfig(provider="typex", model="zai-org/GLM-5-FP8", reasoning_effort="off"),
    )
    assert isinstance(typex_off, DeepSeekChatOpenAI)
    assert typex_off.extra_body == {"thinking": {"type": "disabled"}}

    # typex with non-reasoning model: no params
    typex_plain = create_chat_model(
        "test-key",
        ModelConfig(provider="typex", model="some-plain-model", reasoning_effort="high"),
    )
    assert isinstance(typex_plain, DeepSeekChatOpenAI)
    assert typex_plain.extra_body is None


def test_qwen_reasoning_uses_enable_thinking_format():
    """Qwen uses enable_thinking + thinking_budget via extra_body (deepseek protocol)."""
    qwen = create_chat_model(
        "test-key",
        ModelConfig(provider="qwen", model="qwen3-max", reasoning_effort="high"),
    )
    assert isinstance(qwen, DeepSeekChatOpenAI)
    assert qwen.extra_body == {"enable_thinking": True, "thinking_budget": 8191}

    qwen_off = create_chat_model(
        "test-key",
        ModelConfig(provider="qwen", model="qwen3-max", reasoning_effort="off"),
    )
    assert isinstance(qwen_off, DeepSeekChatOpenAI)
    assert qwen_off.extra_body == {"enable_thinking": False}

    # Qwen with non-reasoning model: no params
    qwen_plain = create_chat_model(
        "test-key",
        ModelConfig(provider="qwen", model="qwen-turbo", reasoning_effort="high"),
    )
    assert isinstance(qwen_plain, DeepSeekChatOpenAI)
    assert qwen_plain.extra_body is None


def test_zhipu_reasoning_uses_thinking_format():
    """Zhipu uses thinking: {type: ...} via extra_body (deepseek protocol)."""
    zhipu = create_chat_model(
        "test-key",
        ModelConfig(provider="zhipu", model="glm-5", reasoning_effort="high"),
    )
    assert isinstance(zhipu, DeepSeekChatOpenAI)
    assert zhipu.extra_body == {"thinking": {"type": "enabled"}}

    zhipu_off = create_chat_model(
        "test-key",
        ModelConfig(provider="zhipu", model="glm-5", reasoning_effort="off"),
    )
    assert isinstance(zhipu_off, DeepSeekChatOpenAI)
    assert zhipu_off.extra_body == {"thinking": {"type": "disabled"}}

    # Zhipu with non-reasoning model: no params
    zhipu_plain = create_chat_model(
        "test-key",
        ModelConfig(provider="zhipu", model="glm-4-flash", reasoning_effort="high"),
    )
    assert isinstance(zhipu_plain, DeepSeekChatOpenAI)
    assert zhipu_plain.extra_body is None


def test_doubao_reasoning_uses_thinking_format():
    """Doubao uses thinking: {type: ...} via extra_body (deepseek protocol)."""
    doubao = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="medium"),
    )
    assert isinstance(doubao, DeepSeekChatOpenAI)
    assert doubao.extra_body == {"thinking": {"type": "enabled"}}

    doubao_off = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="off"),
    )
    assert isinstance(doubao_off, DeepSeekChatOpenAI)
    assert doubao_off.extra_body == {"thinking": {"type": "disabled"}}

    doubao_auto = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="auto"),
    )
    assert isinstance(doubao_auto, DeepSeekChatOpenAI)
    assert doubao_auto.extra_body == {"thinking": {"type": "auto"}}

    # Doubao with non-reasoning model: no params
    doubao_plain = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-lite", reasoning_effort="medium"),
    )
    assert isinstance(doubao_plain, DeepSeekChatOpenAI)
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


def test_extract_thinking_from_deepseek_protocol_chunks():
    """DeepSeek protocol uses the same OpenAI-compatible extraction path."""
    assert extract_thinking(
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "china thinking"}),
        "deepseek",
    ) == "china thinking"


def test_deepseek_chat_preserves_reasoning_content_in_streaming_chunks():
    """DeepSeekChatOpenAI should inject reasoning_content from raw delta into additional_kwargs."""
    model = DeepSeekChatOpenAI(api_key="test-key", model="deepseek-reasoner")

    # Simulate a raw streaming chunk from DeepSeek
    raw_chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Let me think about this...",
                },
                "finish_reason": None,
            }
        ],
    }

    generation = model._convert_chunk_to_generation_chunk(raw_chunk, AIMessageChunk, None)
    assert generation is not None
    msg = generation.message
    assert isinstance(msg, AIMessageChunk)
    assert msg.additional_kwargs.get("reasoning_content") == "Let me think about this..."

    # Verify extract_thinking can now find it
    assert extract_thinking(msg, "deepseek") == "Let me think about this..."


def test_deepseek_chat_accumulates_reasoning_content_across_chunks():
    """Multiple streaming chunks should accumulate reasoning_content."""
    model = DeepSeekChatOpenAI(api_key="test-key", model="deepseek-reasoner")

    chunk1 = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": "", "reasoning_content": "First "},
            "finish_reason": None,
        }],
    }
    chunk2 = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": "", "reasoning_content": "Second"},
            "finish_reason": None,
        }],
    }

    gen1 = model._convert_chunk_to_generation_chunk(chunk1, AIMessageChunk, None)
    assert gen1.message.additional_kwargs.get("reasoning_content") == "First "

    gen2 = model._convert_chunk_to_generation_chunk(chunk2, AIMessageChunk, None)
    assert gen2.message.additional_kwargs.get("reasoning_content") == "Second"


def test_deepseek_chat_no_reasoning_content_passes_through():
    """Chunks without reasoning_content should pass through unchanged."""
    model = DeepSeekChatOpenAI(api_key="test-key", model="deepseek-reasoner")

    raw_chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": "Hello!"},
            "finish_reason": None,
        }],
    }

    generation = model._convert_chunk_to_generation_chunk(raw_chunk, AIMessageChunk, None)
    assert generation is not None
    assert "reasoning_content" not in generation.message.additional_kwargs


def test_deepseek_chat_reasoning_kwargs_maps_effort_per_provider():
    """DeepSeekChatOpenAI.reasoning_kwargs maps unified effort to provider-specific formats."""
    # DeepSeek: xhigh → max, medium → high
    ds_xhigh = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="xhigh"),
    )
    assert ds_xhigh == {"reasoning_effort": "max", "extra_body": {"thinking": {"type": "enabled"}}}

    ds_medium = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="medium"),
    )
    assert ds_medium == {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}

    ds_none = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="none"),
    )
    assert ds_none == {"extra_body": {"thinking": {"type": "disabled"}}}

    # Qwen: high → thinking_budget
    qwen_high = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="qwen", model="qwen3-max", reasoning_effort="high"),
    )
    assert qwen_high == {"extra_body": {"enable_thinking": True, "thinking_budget": 8191}}

    # Zhipu: only enabled/disabled
    zhipu_high = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="zhipu", model="glm-5", reasoning_effort="high"),
    )
    assert zhipu_high == {"extra_body": {"thinking": {"type": "enabled"}}}

    # Mimo: only enabled/disabled
    mimo_medium = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="medium"),
    )
    assert mimo_medium == {"extra_body": {"thinking": {"type": "enabled"}}}

    # Doubao: auto → auto, none → disabled
    doubao_auto = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="auto"),
    )
    assert doubao_auto == {"extra_body": {"thinking": {"type": "auto"}}}

    doubao_none = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="none"),
    )
    assert doubao_none == {"extra_body": {"thinking": {"type": "disabled"}}}

    # Kimi: medium → enabled
    kimi_medium = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="moonshot-v1", reasoning_effort="medium"),
    )
    assert kimi_medium == {"extra_body": {"thinking": {"type": "enabled"}}}

    # Unknown provider with deepseek protocol falls back to DeepSeek format
    custom_xhigh = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="my-custom-ds", model="some-model", reasoning_effort="xhigh"),
    )
    assert custom_xhigh == {"reasoning_effort": "max", "extra_body": {"thinking": {"type": "enabled"}}}

    custom_none = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="my-custom-ds", model="some-model", reasoning_effort="none"),
    )
    assert custom_none == {"extra_body": {"thinking": {"type": "disabled"}}}

    # MiniMax: enabled + reasoning_split
    minimax_high = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="minimax", model="MiniMax-M3", reasoning_effort="high"),
    )
    assert minimax_high == {"extra_body": {"thinking": {"type": "enabled"}, "reasoning_split": True}}

    minimax_none = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="minimax", model="MiniMax-M3", reasoning_effort="none"),
    )
    assert minimax_none == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_deepseek_chat_preserves_reasoning_details_in_streaming_chunks():
    """MiniMax-style reasoning_details in streaming delta should be preserved."""
    model = DeepSeekChatOpenAI(api_key="test", model="MiniMax-M3")

    raw_chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "content": "",
                "reasoning_details": [{"type": "reasoning.text", "text": "Let me think..."}],
            },
            "finish_reason": None,
        }],
    }

    generation = model._convert_chunk_to_generation_chunk(raw_chunk, AIMessageChunk, None)
    assert generation is not None
    rd = generation.message.additional_kwargs.get("reasoning_details")
    assert isinstance(rd, list)
    assert len(rd) == 1
    assert rd[0]["text"] == "Let me think..."


def test_deepseek_chat_accumulates_reasoning_details_across_chunks():
    """MiniMax-style reasoning_details should accumulate across streaming chunks."""
    model = DeepSeekChatOpenAI(api_key="test", model="MiniMax-M3")

    chunk1 = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {
                "reasoning_details": [{"type": "reasoning.text", "text": "Part1"}],
            },
            "finish_reason": None,
        }],
    }
    chunk2 = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {
                "reasoning_details": [{"type": "reasoning.text", "text": "Part2"}],
            },
            "finish_reason": None,
        }],
    }

    gen1 = model._convert_chunk_to_generation_chunk(chunk1, AIMessageChunk, None)
    gen2 = model._convert_chunk_to_generation_chunk(chunk2, AIMessageChunk, None)

    rd1 = gen1.message.additional_kwargs.get("reasoning_details")
    assert isinstance(rd1, list)
    assert len(rd1) == 1
    assert rd1[0]["text"] == "Part1"

    rd2 = gen2.message.additional_kwargs.get("reasoning_details")
    assert isinstance(rd2, list)
    assert len(rd2) == 1
    assert rd2[0]["text"] == "Part2"
