from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage

from tests.test_agent.adapters.langgraph.runtime.test_subagent_task_state_retention import (
    _observe_policy, _goal_resolution, _contract, _FakeUiPort,
)
from tests.tool_registry import build_registry
from voidx.agent.adapters.langgraph.runtime import subagent as module
from voidx.agent.application.agents import AgentDef
from voidx.agent.domain.profile import CHAT_PROFILE
from voidx.config import Config


async def invoke(tmp_path, **kwargs):
    return await module.run_subagent(
        AgentDef(name="voidx", description="test", when_to_use="test", can_write=True, can_delegate=False),
        "Perform task", "test-key", Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(), result_contract=_contract(),
        parent_tools=build_registry(), debug=False, ui_port=_FakeUiPort(), **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reason", [("message", "task_message"), ("question", "task_message"), ("answer", "task_message"), ("result", "unchanged"), ("lifecycle", "unchanged")])
async def test_inbox_once(tmp_path, monkeypatch, kind, reason):
    policies = _observe_policy(monkeypatch, call_interval=99, token_interval=999999)
    item = SimpleNamespace(message_id="stable", session_id="session", source_run_id="parent", target_run_id="child", type=kind, payload={"content": "unique supplement"})
    gateway = Mock()
    gateway.lookup_run.return_value = SimpleNamespace(parent_run_id="parent", session_id="session", status="running")
    gateway.list_child_runs.return_value = []
    gateway.receive = AsyncMock(side_effect=[[], [item, item], [item]])
    gateway.backfill_result = AsyncMock()
    gateway.send = AsyncMock()
    requests = []

    async def stream(*args, **kwargs):
        requests.append(list(args[1]))
        if len(requests) < 3:
            return AIMessage(content="", tool_calls=[{"name": "read", "args": {"file_path": str(tmp_path / "missing")}, "id": str(len(requests)), "type": "tool_call"}])
        return AIMessage(content="done")

    monkeypatch.setattr(module, "stream_llm", stream)
    await invoke(tmp_path, agent_gateway=gateway, agent_run_id="child")
    assert [d.reason for d in policies[0].commits] == ["initial", reason, "unchanged"]
    assert sum(str(m.content).count("unique supplement") for m in requests[-1]) == 1
    gateway.receive.assert_called_with(run_id="child", limit=100, timeout=0)


@pytest.mark.asyncio
async def test_child_own_profile_suppresses_real_request(tmp_path, monkeypatch):
    _observe_policy(monkeypatch)
    requests = []

    async def stream(*args, **kwargs):
        requests.append(list(args[1]))
        return AIMessage(content="done")

    monkeypatch.setattr(module, "stream_llm", stream)
    await invoke(tmp_path, runtime_profile=CHAT_PROFILE)
    assert requests
    assert all("## Current Task State" not in str(m.content) for m in requests[0])


@pytest.mark.asyncio
async def test_paused_invocations_keep_independent_policy(tmp_path, monkeypatch):
    import asyncio

    policies = _observe_policy(monkeypatch)
    entered = asyncio.Event()
    resume = asyncio.Event()
    requests = []

    async def stream(*args, **kwargs):
        requests.append(list(args[1]))
        if len(requests) == 2:
            entered.set()
        await resume.wait()
        return AIMessage(content="done")

    monkeypatch.setattr(module, "stream_llm", stream)
    tasks = [asyncio.create_task(invoke(tmp_path, agent_run_id=run)) for run in ("a", "b")]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert len(policies) == 2
        assert all(not policy.commits for policy in policies)
        resume.set()
        assert await asyncio.gather(*tasks) == ["done", "done"]
        assert policies[0] is not policies[1]
        assert [[d.reason for d in policy.commits] for policy in policies] == [["initial"], ["initial"]]
        assert all(sum("## Current Task State" in str(m.content) for m in request) == 1 for request in requests)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_resume_preserves_established_baselines(tmp_path, monkeypatch):
    import asyncio

    policies = _observe_policy(monkeypatch)
    entered = asyncio.Event()
    resume = asyncio.Event()
    requests = {}
    (tmp_path / "baseline").write_text("baseline")

    async def stream(*args, **kwargs):
        task = asyncio.current_task()
        frames = requests.setdefault(task, [])
        frames.append(list(args[1]))
        if len(frames) == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "read", "args": {"file_path": str(tmp_path / "baseline")},
                "id": "baseline", "type": "tool_call",
            }])
        if len(requests) == 2 and all(len(frames) == 2 for frames in requests.values()):
            entered.set()
        await resume.wait()
        return AIMessage(content="done")

    monkeypatch.setattr(module, "stream_llm", stream)
    tasks = [asyncio.create_task(invoke(tmp_path, agent_run_id=run)) for run in ("a", "b")]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        baselines = [policy.state for policy in policies]
        assert len(baselines) == 2
        assert all(state.snapshot is not None and state.calls_since_snapshot == 0 for state in baselines)
        assert all([d.reason for d in policy.commits] == ["initial"] for policy in policies)
        resume.set()
        assert await asyncio.gather(*tasks) == ["done", "done"]
        for policy, baseline in zip(policies, baselines):
            assert [d.reason for d in policy.commits] == ["initial", "unchanged"]
            assert policy.commits[-1].baseline is baseline
            assert policy.state.calls_since_snapshot == 1
        assert policies[0].state is not policies[1].state
        for frames in requests.values():
            snapshot = next(m for m in frames[0] if m.additional_kwargs.get("_voidx_task_state_snapshot"))
            assert frames[1][frames[0].index(snapshot)].content == snapshot.content
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
