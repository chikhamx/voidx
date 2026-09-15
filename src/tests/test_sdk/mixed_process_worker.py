"""Production process harness: scripted provider and observational lock tracing only."""
import asyncio
import json
import os
import shlex
import sys
import time
from contextlib import suppress
from pathlib import Path

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import PrivateAttr

from tests.test_sdk.test_headless_runtime import FileRoundTripModel


def trace(directory, role, event, **fields):
    with (directory / f'{role}.events.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(pid=os.getpid(), time=time.monotonic_ns(), event=event, **fields)) + '\n')


class ProcessWriterModel(FileRoundTripModel):
    _directory: Path = PrivateAttr()
    _role: str = PrivateAttr()
    _legacy_replies: int = PrivateAttr(default=0)

    async def _astream(self, messages, **kwargs):
        sdk = any(isinstance(m, HumanMessage) and 'SDK_PROCESS_WRITE' in str(m.content) for m in messages)
        role = 'sdk' if sdk else 'legacy'
        trace(self._directory, self._role, 'history', writer=role,
              messages=[dict(type=m.type, content=str(m.content), calls=getattr(m, 'tool_calls', []),
                             call_id=getattr(m, 'tool_call_id', None)) for m in messages])
        calls = []
        if not sdk:
            if not any(isinstance(m, ToolMessage) and m.tool_call_id == 'loop-prime' for m in messages):
                calls = [dict(id='loop-prime', name='bash', args=dict(command='printf prime > legacy-prime.txt'))]
            elif self._legacy_replies == 0:
                self._legacy_replies += 1
                calls = [dict(id='loop-next', name='loop_commit', args=dict(outcome='continue',
                    summary='Prime complete', progress='meaningful', next_delay_seconds=1))]
            elif self._legacy_replies == 1:
                self._legacy_replies += 1
                yield ChatGenerationChunk(message=AIMessageChunk(content='Iteration complete.'))
                return
            else:
                trace(self._directory, self._role, 'background_ready')
                while not (self._directory / 'allow-legacy').exists():
                    await asyncio.sleep(.02)
        if not calls:
            call_id = f'{role}-fresh-write'
            if not any(isinstance(m, ToolMessage) and m.tool_call_id == call_id for m in messages):
                command = f'printf {role}-fresh > {role}-fresh.txt'
                if sdk:
                    release = shlex.quote(str(self._directory / 'release-sdk'))
                    command += f'; while [ ! -f {release} ]; do sleep 0.1; done'
                calls = [dict(id=call_id, name='bash', args=dict(command=command))]
                trace(self._directory, self._role, 'fresh_requested', writer=role, call_id=call_id)
            else:
                trace(self._directory, self._role, 'fresh_returned', writer=role)
                while not (self._directory / f'release-{role}').exists():
                    await asyncio.sleep(.02)
        yield ChatGenerationChunk(message=AIMessageChunk(content='' if calls else 'Written.', tool_calls=calls))


class GoalProcessWriterModel(ProcessWriterModel):
    _work_primed: bool = PrivateAttr(default=False)

    async def _astream(self, messages, **kwargs):
        human_text = '\n'.join(str(m.content) for m in messages if isinstance(m, HumanMessage))
        if 'SDK_PROCESS_WRITE' in human_text:
            async for chunk in super()._astream(messages, **kwargs):
                yield chunk
            return
        evaluator = '## Durable WorkCheckpoint' in human_text
        phase = 'evaluator' if evaluator else 'work'
        trace(self._directory, self._role, 'goal_history', phase=phase,
              messages=[dict(type=m.type, content=str(m.content), calls=getattr(m, 'tool_calls', []),
                             call_id=getattr(m, 'tool_call_id', None)) for m in messages])
        if evaluator:
            calls = [dict(id='goal-evaluate', name='goal_decision', args=dict(
                status='continue', summary='EVALUATOR_ONLY: prime verified; fresh write required',
                evidence=['legacy-prime.txt'], progress='meaningful', next_hint='Write fresh evidence'))]
        elif not self._work_primed:
            if any(isinstance(m, ToolMessage) and m.tool_call_id == 'goal-prime' for m in messages):
                self._work_primed = True
                calls = [dict(id='goal-checkpoint', name='goal_checkpoint', args=dict(
                    summary='Prime exists', evidence=['legacy-prime.txt'], progress='meaningful'))]
            else:
                calls = [dict(id='goal-prime', name='bash', args=dict(command='printf prime > legacy-prime.txt'))]
        else:
            trace(self._directory, self._role, 'background_ready')
            while not (self._directory / 'allow-legacy').exists():
                await asyncio.sleep(.02)
            if any(isinstance(m, ToolMessage) and m.tool_call_id == 'legacy-fresh-write' for m in messages):
                trace(self._directory, self._role, 'fresh_returned', writer='legacy')
                yield ChatGenerationChunk(message=AIMessageChunk(content='WORK_ONLY: written'))
                return
            release = shlex.quote(str(self._directory / 'release-legacy'))
            calls = [dict(id='legacy-fresh-write', name='bash', args=dict(command=
                f'printf legacy-fresh > legacy-fresh.txt; while [ ! -f {release} ]; do sleep 0.1; done'))]
            trace(self._directory, self._role, 'fresh_requested', writer='legacy', call_id='legacy-fresh-write')
        yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_calls=calls))


async def serve(workspace, directory, role):
    from voidx.persistence import sqlite as store
    from voidx.config import Config, PermissionMode, Settings
    from voidx.config.adapters.profile_store import MemoryModelProfileStore
    from voidx.llm.adapters import langchain_model_factory
    from voidx.bootstrap.agent import build_agent_app
    from voidx.presentation.terminal import run_loop
    from voidx.agent.adapters.persistence.headless_locks import OwnedWorkspaceWriteLock, HeadlessWorkspaceWriteLock
    from voidx.agent.adapters.persistence.session_repository import create_session

    store.DATA_DIR = workspace / '.voidx'
    goal = os.environ.get('S6_GOAL_PROCESS') == '1'
    model = GoalProcessWriterModel() if goal else ProcessWriterModel()
    model._directory, model._role = directory, role
    langchain_model_factory.create_chat_model = lambda *a, **kw: model
    langchain_model_factory.create_resolver_model = lambda *a, **kw: model
    from voidx.tooling.builtin.shell.bash.tool import BashTool
    tool_tasks, lock_owners, cancellation_acks = [], [], []
    execute = BashTool.execute

    async def observed_execute(self, args, ctx):
        if goal and 'legacy-fresh.txt' in args.get('command', ''):
            tool_tasks.append(asyncio.current_task())
            try:
                return await execute(self, args, ctx)
            except asyncio.CancelledError:
                cancellation_acks.append(True)
                trace(directory, role, 'tool_cancel_ack')
                raise
        return await execute(self, args, ctx)

    BashTool.execute = observed_execute
    acquire, close = OwnedWorkspaceWriteLock._acquire, OwnedWorkspaceWriteLock.close

    async def observed_acquire(self, child, thread_id):
        lock_owners.append(self)
        trace(directory, role, 'lock_attempt', thread_id=thread_id, path=str(self._lock._path))
        result = await acquire(self, child, thread_id)
        trace(directory, role, 'lock_acquired', thread_id=thread_id, path=str(self._lock._path))
        return result

    def observed_close(self):
        held = self._lock._handle is not None
        close(self)
        trace(directory, role, 'lock_closed', held=held, path=str(self._lock._path))

    sdk_acquire = HeadlessWorkspaceWriteLock.acquire_workspace_write_lock
    sdk_release = HeadlessWorkspaceWriteLock.release_workspace_write_lock

    async def observed_sdk_acquire(self, thread_id):
        trace(directory, role, 'lock_attempt', thread_id=thread_id, path=str(self._path))
        result = await sdk_acquire(self, thread_id)
        trace(directory, role, 'lock_acquired', thread_id=thread_id, path=str(self._path))
        return result

    def observed_sdk_release(self, thread_id):
        held = self._handle is not None
        sdk_release(self, thread_id)
        trace(directory, role, 'lock_closed', held=held, path=str(self._path))

    HeadlessWorkspaceWriteLock.acquire_workspace_write_lock = observed_sdk_acquire
    HeadlessWorkspaceWriteLock.release_workspace_write_lock = observed_sdk_release
    OwnedWorkspaceWriteLock._acquire = observed_acquire
    OwnedWorkspaceWriteLock.close = observed_close
    run_loop.emit_web_gateway_bootstrap = lambda url: (directory / f'{role}.url').write_text(url)
    config = Config(workspace=str(workspace), permission_mode=PermissionMode.PROJECT_TRUSTED,
                    lsp_format_after_edit=False)
    settings = Settings(str(workspace), profile_store=MemoryModelProfileStore())
    settings.set_runtime_api_key(config.model.provider, 'local-test')
    parent = await create_session(workspace=str(workspace), profile='coding')
    trace(directory, role, 'boot', parent_id=parent.id, workspace=str(workspace), data_dir=str(store.DATA_DIR))
    from voidx.bootstrap.production_sdk_gateway import ProductionSdkGateway
    gateways = []
    gateway_init = ProductionSdkGateway.__init__

    def observed_gateway(self, *args, **kwargs):
        gateway_init(self, *args, **kwargs)
        gateways.append(self)

    ProductionSdkGateway.__init__ = observed_gateway
    app = build_agent_app(config, 'local-test', session=parent, settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    try:
        while not (directory / f'shutdown-{role}').exists():
            if task.done():
                await task
            host = app._run_loop._sessions._host
            if goal and role == 'legacy' and (directory / 'start-goal').exists():
                from voidx.agent.domain.automation.goal import GoalSpec
                (directory / 'start-goal').unlink()
                await host.goal_service.start(parent.id, GoalSpec(
                    objective='Write goal process evidence', acceptance_condition='Fresh evidence exists',
                    max_attempts=3))
                trace(directory, role, 'goal_started')
            if (directory / 'stop-legacy').exists() and role in ('legacy', 'same'):
                service = host.goal_service if goal else host.loop_service
                pump = service._scheduler._pump_task
                stopped = await service.stop(parent.id)
                if goal:
                    trace(directory, role, 'goal_stop_resources',
                          pump_done=pump is not None and pump.done(),
                          pump_cancelling=pump.cancelling() if pump else 0,
                          tools=[dict(done=t.done(), cancelling=t.cancelling()) for t in tool_tasks],
                          tool_cancel_acks=len(cancellation_acks),
                          held_owners=sum(o._lock._handle is not None for o in lock_owners))
                trace(directory, role, 'goal_stopped' if goal else 'loop_stopped', stopped=stopped)
                (directory / 'stop-legacy').unlink()
            if goal and role == 'legacy' and (directory / 'dump-history').exists():
                from voidx.agent.adapters.persistence.session_repository import load_messages
                bindings = await store.fetch_all('SELECT * FROM goal_generations')
                histories = {}
                for binding in bindings:
                    for key in ('main_session_id', 'work_session_id', 'evaluator_session_id'):
                        sid = binding[key]
                        histories[sid] = [dict(role=m.role, content=m.content, tool_calls=m.tool_calls,
                                               tool_call_id=m.tool_call_id) for m in await load_messages(sid)]
                (directory / 'histories.json').write_text(json.dumps(histories))
                (directory / 'dump-history').unlink()
            if gateways:
                trace(directory, role, 'runtime', **gateways[-1].session._run_manager.runtime_snapshot())
            await asyncio.sleep(.02)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


if __name__ == '__main__':
    asyncio.run(serve(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
