import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.subagent_compaction import compact_run_history
from voidx.llm.compaction.service import validate_closed_tool_batches


def history():
    return [HumanMessage(content="Full task: preserve every constraint"),
            AIMessage(content="old findings " * 2000),
            HumanMessage(content="Parent constraint: never write", id="parent:one"),
            AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": "a"}]),
            ToolMessage(content="latest evidence", tool_call_id="a")]


@pytest.mark.asyncio
async def test_compaction_preserves_tasks_tail_and_excludes_overlays():
    source = history()
    source.insert(2, SystemMessage(content="secret system overlay"))
    source.insert(3, HumanMessage(content="old snapshot", additional_kwargs={"_voidx_task_state_snapshot": True}))
    before = list(source)
    async def summarize(request, output_limit):
        text = str(request)
        assert "old findings" in text
        assert "old snapshot" not in text
        assert "secret system overlay" not in text
        assert output_limit > 0
        return AIMessage(content="Earlier findings summarized")
    candidate = await compact_run_history(source, model="", context_limit=16000,
                                          hard_budget=12000, summarize=summarize)
    assert candidate is not None
    assert source == before
    assert source[0] in candidate and source[-3] in candidate
    assert candidate[-2:] == source[-2:]
    assert validate_closed_tool_batches(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "grow", "cancel", "no_room", "invalid"])
async def test_compaction_failure_never_mutates_source(failure):
    source = history()
    if failure == "invalid":
        source.pop()
    before = list(source)
    async def summarize(request, output_limit):
        if failure == "raise":
            raise RuntimeError("summary unavailable")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return AIMessage(content="huge " * 30000)
    kwargs = dict(model="", context_limit=16000, hard_budget=1 if failure == "no_room" else 12000,
                  summarize=summarize)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await compact_run_history(source, **kwargs)
    else:
        assert await compact_run_history(source, **kwargs) is None
    assert source == before


@pytest.mark.asyncio
async def test_recompaction_replaces_prior_summary():
    source = history()
    prior = HumanMessage(content="prior memory " * 2000,
                         additional_kwargs={"_voidx_compaction_summary": True})
    source.insert(1, prior)
    async def summarize(request, output_limit):
        assert "prior memory" in str(request)
        return AIMessage(content="merged findings")
    candidate = await compact_run_history(source, model="", context_limit=16000,
                                          hard_budget=12000, summarize=summarize)
    assert candidate is not None
    assert prior not in candidate
    assert sum(bool(m.additional_kwargs.get("_voidx_compaction_summary")) for m in candidate) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["budget", "provider", "retry", "summary_failure", "overflow_again", "full_prompt", "guidance", "cancel", "expired", "summary_timeout", "provider_expired", "guidance_expired"])
async def test_actual_child_request_continues_after_compaction(tmp_path, monkeypatch, trigger):
    import voidx.agent.adapters.langgraph.runtime.subagent as runtime
    from tests.test_agent.adapters.langgraph.runtime.test_subagent_step_budget_final import (
        _subagent_contract_kwargs, get_agent, Config,
    )
    requests = []
    summaries = []
    metadata = {}
    commits = []
    from types import SimpleNamespace
    clock = [0.0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0))
    original_commit = runtime.TaskStateReminderPolicy.commit
    def commit(policy, prepared, **kwargs):
        commits.append(prepared.reason)
        return original_commit(policy, prepared, **kwargs)
    monkeypatch.setattr(runtime.TaskStateReminderPolicy, "commit", commit)
    class Model:
        def bind_tools(self, tools):
            return self
        def bind(self, *, max_tokens):
            summaries.append({"max_tokens": max_tokens})
            return self
    async def stream(model, messages, renderer, protocol, **kwargs):
        if str(messages[0].content).startswith("Summarize earlier child-run"):
            if trigger == "summary_failure":
                raise RuntimeError("summary unavailable")
            assert not any(m.additional_kwargs.get("_voidx_task_state_snapshot") for m in messages)
            if trigger == "cancel":
                raise asyncio.CancelledError()
            if trigger in {"expired", "summary_timeout", "provider_expired", "guidance_expired"}:
                clock[0] = 1000000
                if trigger == "summary_timeout":
                    raise TimeoutError("summary deadline")
            return AIMessage(content="compressed findings", usage_metadata={"input_tokens": 37, "output_tokens": 11, "total_tokens": 48})
        assert clock[0] == 0, "model work after wall-clock expiry"
        requests.append(list(messages))
        if len(requests) == 1:
            return AIMessage(content="old findings " * 2000, usage_metadata={"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}, tool_calls=[
                {"name": "nonexistent", "args": {}, "id": "first"}])
        if len(requests) == 2:
            return AIMessage(content="latest", usage_metadata={"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}, tool_calls=[
                {"name": "nonexistent", "args": {}, "id": "second"}])
        if trigger in {"provider", "retry", "summary_failure", "overflow_again", "full_prompt", "cancel", "provider_expired"} and len(requests) == 3:
            raise RuntimeError("maximum context length exceeded")
        if trigger == "retry" and len(requests) == 4:
            raise TimeoutError("request timed out")
        if trigger == "overflow_again" and len(requests) == 4:
            raise RuntimeError("maximum context length exceeded")
        return AIMessage(usage_metadata={"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}, content="status: continued successfully\nfiles_changed: none\ntests_run: passed\nrisks: none\nfollowups: none")
    def estimate(messages, tools, model):
        if trigger in {"guidance", "guidance_expired"} and len(requests) >= 2 and any("old findings" in str(m.content) for m in messages):
            return 999999 if any(m.additional_kwargs.get(runtime.GUIDANCE_MARKER) for m in messages) else 80000
        if trigger == "full_prompt" and any("compressed findings" in str(m.content) for m in messages):
            return 999999
        return 999999 if trigger in {"budget", "expired", "summary_timeout"} and len(requests) >= 2 and any("old findings" in str(m.content) for m in messages) else (1000 if any("old findings" in str(m.content) for m in messages) else 100)
    monkeypatch.setattr(runtime, "get_context_limit", lambda *a: 100000)
    monkeypatch.setattr(runtime, "_llm_retry_sleep_delay", lambda delay: 0)
    monkeypatch.setattr(runtime, "create_chat_model", lambda *a, **kw: Model())
    monkeypatch.setattr(runtime, "stream_llm", stream)
    monkeypatch.setattr(runtime, "estimate_context_tokens_with_tools", estimate)
    from voidx.llm.usage import UsageStats
    usage = UsageStats()
    async def run():
        return await runtime.run_subagent(
            get_agent("voidx"), "Preserve full task", "test-key", Config(workspace=str(tmp_path)),
            runtime_persona="explore", usage_stats=usage, run_metadata=metadata,
            **_subagent_contract_kwargs(), debug=False,
        )
    if trigger == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run()
        assert commits == ["initial", "unchanged"]
        return
    result = await run()
    if trigger in {"expired", "summary_timeout", "provider_expired", "guidance_expired"}:
        assert metadata["finish_reason"] == "time_limit"
        assert len(requests) == (3 if trigger == "provider_expired" else 2)
        assert len(summaries) == 1
        assert commits == ["initial", "unchanged"]
        assert usage.total_tokens == (40 if trigger == "summary_timeout" else 88)
        return
    if trigger in {"summary_failure", "overflow_again", "full_prompt"}:
        assert metadata["finish_reason"] == "context_limit"
        assert len(summaries) == 1
        assert len(requests) == (4 if trigger == "overflow_again" else 3)
        assert commits == ["initial", "unchanged"]
        assert usage.total_input_tokens == 26 + (0 if trigger == "summary_failure" else 37)
        assert usage.total_output_tokens == 14 + (0 if trigger == "summary_failure" else 11)
        return
    if trigger == "retry":
        assert requests[3] == requests[4]
    assert "status: continued successfully" in result
    assert len(summaries) == 1
    assert 128 <= summaries[0]["max_tokens"] <= 2048
    assert commits == ["initial", "unchanged", "history_reset"]
    assert len(requests) == {"budget": 3, "provider": 4, "retry": 5, "guidance": 3}[trigger]
    assert any("compressed findings" in str(m.content) for m in requests[-1])
    assert not any("old findings" in str(m.content) for m in requests[-1])
    assert sum(bool(m.additional_kwargs.get("_voidx_task_state_snapshot")) for m in requests[-1]) == 1
    assert metadata["calls"] == len(requests) + 1

    assert usage.total_input_tokens == 3 * 13 + 37
    assert usage.total_output_tokens == 3 * 7 + 11
    assert metadata["tokens"] == 108

@pytest.mark.parametrize("protocol, key", [
    ("openai", "max_tokens"), ("anthropic", "max_tokens"),
    ("deepseek", "max_tokens"), ("gemini", "max_output_tokens"),
])
def test_summary_output_binding_uses_provider_call_parameter(protocol, key):
    from voidx.agent.adapters.langgraph.runtime.subagent_compaction import bind_summary_model

    bound = object()

    class Provider:
        def bind(self, **kwargs):
            assert kwargs == {key: 512}
            return bound

    assert bind_summary_model(Provider(), protocol, 512) is bound
