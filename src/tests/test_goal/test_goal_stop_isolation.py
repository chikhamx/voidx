"""Exact-parent stop through the production web/headless graph and real Bash tool."""
import asyncio
from contextlib import suppress

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.domain.automation.goal import GoalSpec
from voidx.agent.domain.thread import LifecycleState


class TwoParentModel(FileRoundTripModel):
    async def _astream(self, messages, **kwargs):
        text = '\n'.join(str(m.content) for m in messages if isinstance(m, HumanMessage))
        if '## Durable WorkCheckpoint' in text:
            call = dict(id='decision', name='goal_decision', args=dict(
                status='continue', summary='Continue real work', evidence=['checkpoint'],
                progress='meaningful', next_hint='ISOLATION_SECOND_WORK'))
        elif 'ISOLATION_SECOND_WORK' in text:
            call = dict(id='held-bash', name='bash', args=dict(command=
                'printf held > isolation-held.txt; while [ ! -f isolation-release.txt ]; do sleep 0.1; done'))
        else:
            call = dict(id='checkpoint', name='goal_checkpoint', args=dict(
                summary='Initial checkpoint', evidence=['objective'], progress='meaningful'))
        yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_calls=[call]))


@pytest.mark.asyncio
async def test_stop_idle_parent_preserves_other_parent_then_joins_its_tool(tmp_path, monkeypatch):
    from voidx.persistence import sqlite
    from voidx.config import Config, PermissionMode, Settings
    from voidx.config.adapters.profile_store import MemoryModelProfileStore
    from voidx.llm.adapters import langchain_model_factory
    from voidx.bootstrap.agent import build_agent_app
    from voidx.presentation.terminal import run_loop
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.tooling.builtin.shell.bash.tool import BashTool
    from voidx.agent.adapters.persistence.headless_locks import OwnedWorkspaceWriteLock

    monkeypatch.setattr(sqlite, 'DATA_DIR', tmp_path / '.voidx')
    model = TwoParentModel()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **k: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **k: model)
    urls, tools, cancelled, owners = [], [], [], []
    monkeypatch.setattr(run_loop, 'emit_web_gateway_bootstrap', urls.append)
    execute, acquire = BashTool.execute, OwnedWorkspaceWriteLock._acquire

    async def observed_execute(self, args, ctx):
        if 'isolation-held.txt' in args.get('command', ''):
            tools.append(asyncio.current_task())
            try:
                return await execute(self, args, ctx)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return await execute(self, args, ctx)

    async def observed_acquire(self, child, thread_id):
        owners.append(self)
        return await acquire(self, child, thread_id)

    monkeypatch.setattr(BashTool, 'execute', observed_execute)
    monkeypatch.setattr(OwnedWorkspaceWriteLock, '_acquire', observed_acquire)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path), profile_store=MemoryModelProfileStore())
    settings.set_runtime_api_key(config.model.provider, 'local-test')
    a = await create_session(workspace=str(tmp_path), profile='coding')
    b = await create_session(workspace=str(tmp_path), profile='coding')
    app = build_agent_app(config, 'local-test', session=a, settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    service = app._run_loop._sessions._host.goal_service
    scheduler = service._scheduler
    try:
        async with asyncio.timeout(60):
            while not urls:
                if task.done():
                    await task
                await asyncio.sleep(.02)
            # Only hold scheduling during setup; both initial turns use the real graph.
            start_pump = scheduler.start_pump
            monkeypatch.setattr(scheduler, 'start_pump', lambda: None)
            sa = await service.start(a.id, GoalSpec(objective='Parent A', acceptance_condition='Done'))
            pending = await service._store.list_pending_outbox(sa.goal_thread_id)
            assert len(pending) == 1
            assert await service._store.claim_outbox(pending[0].outbox_id,
                lease_owner='test-hold-idle-evaluator-A', lease_seconds=600)
            sb = await service.start(b.id, GoalSpec(objective='Parent B', acceptance_condition='Done'))
            monkeypatch.setattr(scheduler, 'start_pump', start_pump)
            start_pump()
            while not (tmp_path / 'isolation-held.txt').exists():
                await asyncio.sleep(.02)
            pump = scheduler._pump_task
            before = await service._store.load(sb.goal_thread_id)
            assert tools and not tools[0].done()
            assert await service.stop(a.id)
            after = await service._store.load(sb.goal_thread_id)
            assert not cancelled, 'stop(A) cancelled B real inflight Bash'
            assert scheduler._pump_task is pump and not pump.done()
            assert not tools[0].done()
            assert after.state.lifecycle == before.state.lifecycle
            assert after.state_version == before.state_version
            assert (await service.status(b.id)).generation == sb.generation
            assert (await service._store.load(sa.goal_thread_id)).state.lifecycle == LifecycleState.CANCELLED
            stop_goal = scheduler.stop_goal
            flush_seen_before_deactivate = []

            async def observed_stop_goal(thread_id):
                await stop_goal(thread_id)
                from voidx.agent.adapters.persistence.session_repository import load_messages
                binding = await service._store.get_goal_generation(sb.generation)
                messages = await load_messages(binding.work_session_id)
                assert any(c['id'] == 'held-bash' for m in messages for c in (m.tool_calls or []))
                assert tools[0].done() and cancelled == [True]
                flush_seen_before_deactivate.append(True)

            monkeypatch.setattr(scheduler, 'stop_goal', observed_stop_goal)
            assert await service.stop(b.id)
            assert flush_seen_before_deactivate == [True]
            assert cancelled == [True]
            assert all(t.done() for t in tools)
            assert all(o._lock._handle is None for o in owners)
            assert (await service._store.load(sb.goal_thread_id)).state.lifecycle == LifecycleState.CANCELLED
            binding = await service._store.get_goal_generation(sb.generation)
            from voidx.agent.adapters.persistence.session_repository import load_messages
            messages = await load_messages(binding.work_session_id)
            assert any(c['id'] == 'held-bash' for m in messages for c in (m.tool_calls or [])), messages
    finally:
        (tmp_path / 'isolation-release.txt').touch()
        await scheduler.stop_pump()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
