"""Tests for voidx.observability.request_log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.observability.request_log import (
    _serialize_message,
    _serialize_response,
    log_llm_diagnostic,
    log_llm_exchange,
)


# ── _serialize_message ──────────────────────────────────────────────────


class TestSerializeMessage:
    def test_human_message(self):
        msg = HumanMessage(content="hello")
        result = _serialize_message(msg)
        assert result == {"role": "human", "content": "hello"}

    def test_system_message(self):
        msg = SystemMessage(content="you are helpful")
        result = _serialize_message(msg)
        assert result == {"role": "system", "content": "you are helpful"}

    def test_ai_message_with_tool_calls(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "read", "args": {"file_path": "/tmp/x"}, "id": "tc1"}],
        )
        result = _serialize_message(msg)
        assert result["role"] == "ai"
        assert result["tool_calls"][0]["name"] == "read"
        assert result["tool_calls"][0]["args"] == {"file_path": "/tmp/x"}
        assert result["tool_calls"][0]["id"] == "tc1"

    def test_tool_message(self):
        msg = ToolMessage(content="file contents", tool_call_id="tc1")
        result = _serialize_message(msg)
        assert result == {"role": "tool", "content": "file contents", "tool_call_id": "tc1"}

    def test_list_content_text_blocks(self):
        msg = AIMessage(content=[{"type": "text", "text": "thinking..."}, {"type": "text", "text": "result"}])
        result = _serialize_message(msg)
        assert result["content"] == "thinking...\nresult"

    def test_list_content_mixed(self):
        msg = HumanMessage(content=["plain text", {"type": "text", "text": "extra"}])
        result = _serialize_message(msg)
        assert result["content"] == "plain text\nextra"

    def test_list_content_with_dict_text_key(self):
        msg = AIMessage(content=[{"text": "from text key"}])
        result = _serialize_message(msg)
        assert result["content"] == "from text key"

    def test_list_content_empty_fallback(self):
        msg = AIMessage(content=[{"type": "image", "url": "http://x"}])
        result = _serialize_message(msg)
        assert isinstance(result["content"], str)

    def test_message_with_name(self):
        msg = HumanMessage(content="hi", name="user1")
        result = _serialize_message(msg)
        assert result["name"] == "user1"


# ── _serialize_response ─────────────────────────────────────────────────


class TestSerializeResponse:
    def test_simple_response(self):
        msg = AIMessage(content="done")
        result = _serialize_response(msg)
        assert result == {"content": "done"}

    def test_response_with_tool_calls(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "bash", "args": {"command": "ls"}, "id": "tc2"}],
        )
        result = _serialize_response(msg)
        assert result["tool_calls"][0]["name"] == "bash"
        assert result["tool_calls"][0]["args"] == {"command": "ls"}
        assert result["tool_calls"][0]["id"] == "tc2"

    def test_response_with_usage_metadata(self):
        msg = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )
        result = _serialize_response(msg)
        assert result["usage"] == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}

    def test_response_list_content(self):
        msg = AIMessage(content=[{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}])
        result = _serialize_response(msg)
        assert result["content"] == "part1\npart2"


# ── log_llm_exchange ────────────────────────────────────────────────────


class TestLogLlmExchange:
    def test_writes_jsonl(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        messages = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="hello"),
        ]
        response = AIMessage(content="hi there", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

        log_llm_exchange(messages, response, model="gpt-4", provider="openai", step=1, session_id="s1")

        log_file = tmp_path / "llm_requests.jsonl"
        assert log_file.exists()

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["model"] == "gpt-4"
        assert entry["provider"] == "openai"
        assert entry["step"] == 1
        assert entry["session_id"] == "s1"
        assert entry["ts"]
        assert len(entry["request"]["messages"]) == 2
        assert entry["request"]["messages"][0]["role"] == "system"
        assert entry["request"]["messages"][1]["role"] == "human"
        assert entry["response"]["content"] == "hi there"
        assert entry["response"]["usage"]["input_tokens"] == 10

    def test_appends_multiple_calls(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_exchange(
            [HumanMessage(content="q1")],
            AIMessage(content="a1"),
            model="gpt-4",
            provider="openai",
            step=1,
        )
        log_llm_exchange(
            [HumanMessage(content="q2")],
            AIMessage(content="a2"),
            model="gpt-4",
            provider="openai",
            step=2,
        )

        log_file = tmp_path / "llm_requests.jsonl"
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

        entry2 = json.loads(lines[1])
        assert entry2["step"] == 2
        assert entry2["response"]["content"] == "a2"

    def test_no_session_id_omitted(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_exchange(
            [HumanMessage(content="hi")],
            AIMessage(content="hey"),
            model="gpt-4",
            provider="openai",
            step=0,
        )

        log_file = tmp_path / "llm_requests.jsonl"
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert "session_id" not in entry

    def test_creates_log_dir(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        nested = tmp_path / "deep" / "logs"
        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", nested)

        log_llm_exchange(
            [HumanMessage(content="hi")],
            AIMessage(content="hey"),
            model="gpt-4",
            provider="openai",
            step=0,
        )

        assert (nested / "llm_requests.jsonl").exists()

    def test_serialization_failure_does_not_raise(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        class BadMessage(HumanMessage):
            @property
            def content(self):
                raise RuntimeError("broken")

        log_llm_exchange(
            [BadMessage(content="x")],
            AIMessage(content="ok"),
            model="gpt-4",
            provider="openai",
            step=0,
        )

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        read_only = tmp_path / "readonly"
        read_only.mkdir()
        read_only.chmod(0o444)
        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", read_only)

        log_llm_exchange(
            [HumanMessage(content="hi")],
            AIMessage(content="hey"),
            model="gpt-4",
            provider="openai",
            step=0,
        )


class TestLogLlmDiagnostic:
    def test_writes_jsonl_event(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_diagnostic(
            "goal_resolver_decision",
            intent="general",
            plan_join="",
            fallback_reason="structured_output_error",
        )

        log_file = tmp_path / "llm_requests.jsonl"
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["event"] == "goal_resolver_decision"
        assert entry["intent"] == "general"
        assert entry["plan_join"] == ""
        assert entry["fallback_reason"] == "structured_output_error"


class TestLogLlmExchangeToggle:
    def test_enabled_false_skips_write(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_exchange(
            [HumanMessage(content="hi")],
            AIMessage(content="hey"),
            model="gpt-4",
            provider="openai",
            step=0,
            enabled=False,
        )

        log_file = tmp_path / "llm_requests.jsonl"
        assert not log_file.exists()

    def test_enabled_true_writes(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_exchange(
            [HumanMessage(content="hi")],
            AIMessage(content="hey"),
            model="gpt-4",
            provider="openai",
            step=0,
            enabled=True,
        )

        log_file = tmp_path / "llm_requests.jsonl"
        assert log_file.exists()


class TestLogLlmDiagnosticToggle:
    def test_enabled_false_skips_write(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_diagnostic("test_event", enabled=False, key="value")

        log_file = tmp_path / "llm_requests.jsonl"
        assert not log_file.exists()

    def test_enabled_true_writes(self, tmp_path, monkeypatch):
        from voidx.observability import request_log

        monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

        log_llm_diagnostic("test_event", enabled=True, key="value")

        log_file = tmp_path / "llm_requests.jsonl"
        assert log_file.exists()
