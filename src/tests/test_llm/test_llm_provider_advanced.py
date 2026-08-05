import sys
from pathlib import Path


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
    "kimi": ("deepseek", DeepSeekChatOpenAI, "https://api.kimi.com/coding/v1"),
    "doubao": ("deepseek", DeepSeekChatOpenAI, "https://ark.cn-beijing.volces.com/api/v3"),
    "minimax": ("deepseek", DeepSeekChatOpenAI, "https://api.minimax.io/v1"),
}


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
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="none"),
    )
    assert isinstance(doubao_off, DeepSeekChatOpenAI)
    assert doubao_off.extra_body == {"thinking": {"type": "disabled"}}

    doubao_auto = create_chat_model(
        "test-key",
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="high"),
    )
    assert isinstance(doubao_auto, DeepSeekChatOpenAI)
    assert doubao_auto.extra_body == {"thinking": {"type": "enabled"}}

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

    # Doubao: non-none → enabled, none → disabled (no external auto)
    doubao_on = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="high"),
    )
    assert doubao_on == {"extra_body": {"thinking": {"type": "enabled"}}}

    doubao_none = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="doubao", model="doubao-seed-1.6-thinking", reasoning_effort="none"),
    )
    assert doubao_none == {"extra_body": {"thinking": {"type": "disabled"}}}

    # Kimi: medium → enabled
    kimi_medium = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="moonshot-v1", reasoning_effort="medium"),
    )
    assert kimi_medium == {"extra_body": {"thinking": {"type": "enabled"}}}

    # Unknown provider: deepseek-protocol fallback format, clamped to deepseek ladder
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


def test_deepseek_chat_injects_reasoning_content_into_request_payload():
    """DeepSeek thinking mode requires reasoning_content to be passed back in multi-turn.

    When an AIMessage carries reasoning_content in additional_kwargs (as injected
    by _convert_chunk_to_generation_chunk during streaming), the request payload
    sent to the DeepSeek API must include it as a top-level field on the assistant
    message dict.  LangChain's _convert_message_to_dict silently drops it.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    model = DeepSeekChatOpenAI(api_key="test-key", model="deepseek-reasoner")

    messages = [
        HumanMessage(content="hello"),
        AIMessage(
            content="Hi there",
            additional_kwargs={"reasoning_content": "Let me think about this..."},
        ),
        HumanMessage(content="thanks"),
    ]

    payload = model._get_request_payload(messages)

    assert "messages" in payload
    msgs = payload["messages"]
    assert len(msgs) == 3
    assistant_dict = msgs[1]
    assert assistant_dict["role"] == "assistant"
    assert assistant_dict.get("reasoning_content") == "Let me think about this..."


def test_deepseek_chat_omits_reasoning_content_when_absent():
    """No reasoning_content in additional_kwargs → no reasoning_content key in payload."""
    from langchain_core.messages import AIMessage, HumanMessage

    model = DeepSeekChatOpenAI(api_key="test-key", model="deepseek-reasoner")

    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="Hi there"),
    ]

    payload = model._get_request_payload(messages)
    msgs = payload["messages"]
    assistant_dict = msgs[1]
    assert "reasoning_content" not in assistant_dict



def test_kimi_k3_reasoning_effort_mapping():
    """Kimi K3 maps reasoning effort to reasoning_effort and extra_body.thinking.type."""
    k3_max = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="k3", reasoning_effort="max"),
    )
    assert k3_max == {"reasoning_effort": "max", "extra_body": {"thinking": {"type": "enabled"}}}

    k3_xhigh = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="k3", reasoning_effort="xhigh"),
    )
    assert k3_xhigh == {"reasoning_effort": "max", "extra_body": {"thinking": {"type": "enabled"}}}


    k3_high = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="k3", reasoning_effort="high"),
    )
    assert k3_high == {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}

    k3_medium = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="k3", reasoning_effort="medium"),
    )
    assert k3_medium == {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}

    k3_low = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="k3", reasoning_effort="low"),
    )
    assert k3_low == {"reasoning_effort": "low", "extra_body": {"thinking": {"type": "enabled"}}}

    k3_none = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="k3", reasoning_effort="none"),
    )
    assert k3_none == {"extra_body": {"thinking": {"type": "disabled"}}}

    k26_medium = DeepSeekChatOpenAI.reasoning_kwargs(
        ModelConfig(provider="kimi", model="kimi-k2.6", reasoning_effort="medium"),
    )
    assert k26_medium == {"extra_body": {"thinking": {"type": "enabled"}}}
