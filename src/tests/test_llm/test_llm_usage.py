import json
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage


from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens,
    estimate_context_tokens_with_tools,
    estimate_message_tokens,
    extract_token_usage,
    format_cache_hit_rate,
    format_token_count,
)


def test_extract_token_usage_from_usage_metadata():
    message = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 12,
            "output_tokens": 5,
            "total_tokens": 17,
        },
    )

    usage = extract_token_usage(message)

    assert usage.input_tokens == 12
    assert usage.output_tokens == 5
    assert usage.total_tokens == 17


def test_extract_token_usage_from_openai_response_metadata():
    message = AIMessage(
        content="ok",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 20,
                "completion_tokens": 7,
                "total_tokens": 27,
                "prompt_tokens_details": {"cached_tokens": 8},
            }
        },
    )

    usage = extract_token_usage(message)

    assert usage.input_tokens == 20
    assert usage.output_tokens == 7
    assert usage.total_tokens == 27
    assert usage.cache_read_tokens == 8


def test_usage_stats_records_last_and_session_totals():
    stats = UsageStats(context_limit=128_000)

    stats.update_context(1_234)
    stats.record_call(extract_token_usage(AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )))
    stats.record_call(extract_token_usage(AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
    )))

    assert stats.context_tokens == 20
    assert stats.last_input_tokens == 20
    assert stats.last_output_tokens == 6
    assert stats.total_input_tokens == 30
    assert stats.total_output_tokens == 10
    assert stats.total_tokens == 40
    assert stats.total_calls == 2

    stats.reset()

    assert stats.context_tokens == 0
    assert stats.last_input_tokens == 0
    assert stats.last_output_tokens == 0
    assert stats.total_tokens == 0
    assert stats.total_calls == 0


def test_usage_stats_tracks_current_turn_totals():
    stats = UsageStats(context_limit=128_000)

    stats.record_call(extract_token_usage(AIMessage(
        content="before",
        usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105},
    )))
    stats.begin_turn()
    stats.record_call(extract_token_usage(AIMessage(
        content="first",
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )))
    stats.record_call(extract_token_usage(AIMessage(
        content="second",
        usage_metadata={"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
    )))

    assert stats.turn_calls == 2
    assert stats.turn_input_tokens == 30
    assert stats.turn_output_tokens == 10

    stats.end_turn()

    assert stats.turn_calls == 0
    assert stats.turn_input_tokens == 0
    assert stats.turn_output_tokens == 0


def test_usage_stats_records_cache_hit_rate():
    stats = UsageStats(context_limit=128_000)

    stats.record_call(extract_token_usage(AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 4,
            "total_tokens": 104,
            "input_token_details": {
                "cache_read": 40,
                "cache_creation": 10,
            },
        },
    )))

    assert stats.last_cache_read_tokens == 40
    assert stats.last_cache_write_tokens == 10
    assert stats.total_cache_read_tokens == 40
    assert stats.total_cache_write_tokens == 10
    assert stats.cache_hit_rate == 0.4
    assert format_cache_hit_rate(stats) == "40%"
    stats.reset()
    assert format_cache_hit_rate(stats) == "--"


def test_usage_stats_estimates_cache_hit_rate_when_provider_does_not_report_cache():
    stats = UsageStats(context_limit=128_000)
    shared = HumanMessage(content="shared project context")
    first_messages = [shared]
    second_messages = [shared, HumanMessage(content="new request")]

    stats.record_call(
        extract_token_usage(AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 100, "output_tokens": 4, "total_tokens": 104},
        )),
        messages=first_messages,
        model="test-model",
        cache_key="test/test-model",
    )
    stats.record_call(
        extract_token_usage(AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 100, "output_tokens": 4, "total_tokens": 104},
        )),
        messages=second_messages,
        model="test-model",
        cache_key="test/test-model",
    )

    expected = estimate_context_tokens(first_messages, "test-model")
    assert stats.last_estimated_cache_read_tokens == expected
    assert stats.total_estimated_cache_read_tokens == expected
    assert stats.cache_hit_rate_is_estimated is True
    assert format_cache_hit_rate(stats).startswith("~")


def test_usage_stats_prefers_provider_cache_hit_rate_over_estimate():
    stats = UsageStats(context_limit=128_000)
    shared = HumanMessage(content="shared project context")

    stats.record_call(
        extract_token_usage(AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 4,
                "total_tokens": 104,
                "input_token_details": {"cache_read": 0},
            },
        )),
        messages=[shared],
        model="test-model",
        cache_key="test/test-model",
    )

    assert stats.cache_hit_rate == 0
    assert stats.cache_hit_rate_is_estimated is False
    assert format_cache_hit_rate(stats) == "0%"


def test_estimate_context_tokens_ignores_image_payload_bytes():
    messages = [
        HumanMessage(content=[
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 10_000}},
        ])
    ]

    tokens = estimate_context_tokens(messages)

    assert tokens < 20



def test_estimate_message_tokens_includes_raw_additional_kwargs_tool_calls():
    message = AIMessage(
        content="",
        tool_calls=[],
        additional_kwargs={
            "tool_calls": [{
                "id": "edit-1",
                "name": "replace",
                "args": {"file_path": "f.py", "new_string": " token" * 5000},
            }],
        },
    )

    assert estimate_message_tokens(message, "test-model") > 4096


def test_estimate_message_tokens_includes_content_tool_use_blocks():
    message = AIMessage(
        content=[{
            "type": "tool_use",
            "id": "edit-1",
            "name": "replace",
            "input": {"file_path": "f.py", "new_string": " token" * 5000},
        }],
        tool_calls=[],
    )

    assert estimate_message_tokens(message, "test-model") > 4096



def test_estimate_message_tokens_uses_larger_raw_args_for_duplicate_id():
    message = AIMessage(
        content="",
        tool_calls=[{
            "id": "edit-1",
            "name": "replace",
            "args": {},
            "type": "tool_call",
        }],
        additional_kwargs={
            "tool_calls": [{
                "id": "edit-1",
                "type": "function",
                "function": {
                    "name": "replace",
                    "arguments": json.dumps({
                        "file_path": "f.py",
                        "new_string": " token" * 5000,
                    }),
                },
            }],
        },
    )

    assert estimate_message_tokens(message, "test-model") > 4096


def test_estimate_message_tokens_uses_larger_content_args_for_duplicate_id():
    message = AIMessage(
        content=[{
            "type": "tool_use",
            "id": "edit-1",
            "name": "replace",
            "input": {
                "file_path": "f.py",
                "new_string": " token" * 5000,
            },
        }],
        tool_calls=[{
            "id": "edit-1",
            "name": "replace",
            "args": {},
            "type": "tool_call",
        }],
    )

    assert estimate_message_tokens(message, "test-model") > 4096

def test_estimate_message_tokens_deduplicates_tool_call_representations_by_id():
    canonical = {
        "id": "edit-1",
        "name": "replace",
        "args": {"file_path": "f.py", "new_string": "replacement"},
        "type": "tool_call",
    }
    canonical_only = AIMessage(content="", tool_calls=[canonical])
    duplicated = AIMessage(
        content=[{
            "type": "tool_use",
            "id": "edit-1",
            "name": "replace",
            "input": {"file_path": "f.py", "new_string": "replacement"},
        }],
        tool_calls=[canonical],
        additional_kwargs={
            "tool_calls": [{
                "id": "edit-1",
                "name": "replace",
                "args": {"file_path": "f.py", "new_string": "replacement"},
            }],
        },
    )

    assert estimate_message_tokens(duplicated, "test-model") == estimate_message_tokens(
        canonical_only,
        "test-model",
    )

def test_estimate_context_tokens_with_tools_includes_tool_schema():
    messages = [HumanMessage(content="hello")]
    tool_defs = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from disk",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                },
                "required": ["path"],
            },
        },
    }]

    messages_only = estimate_context_tokens(messages)
    with_tools = estimate_context_tokens_with_tools(messages, tool_defs)

    assert estimate_context_tokens_with_tools(messages, []) == messages_only
    assert with_tools > messages_only


def test_format_token_count_uses_compact_suffixes():
    assert format_token_count(999) == "999"
    assert format_token_count(1_000) == "1k"
    assert format_token_count(1_250) == "1.2k"
    assert format_token_count(1_000_000) == "1m"
