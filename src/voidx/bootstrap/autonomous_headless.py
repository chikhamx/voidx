"""Production automation composition without presentation or a second execution loop."""
from dataclasses import dataclass

from voidx.agent.adapters.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.adapters.null_events import NullEventPublisher
from voidx.agent.adapters.persistence.memory_session import MemorySessionAdapter
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.automation.goal.evaluator import GoalEvaluator
from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.application.automation.goal.scheduler import GoalRuntimeScheduler
from voidx.agent.application.automation.loop.loop_service import LoopService
from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
from voidx.agent.application.guidance_service import GuidanceService
from voidx.agent.application.runtime.autonomous_session import SessionRunCoordinator
from voidx.agent.application.runtime.runtime import AgentRuntime


def _resolve_session_profile(session, workspace):
    from voidx.agent.application.agent_registry import agent_registry_for
    from voidx.agent.application.agent_profile_snapshot import restore_session_profile

    return restore_session_profile(
        agent_registry_for(workspace), profile_id=session.runtime_profile, snapshot=session.profile_snapshot,
    )


async def _validate_recovery_child(store, child_id, session, workspace, snapshot):
    from voidx.persistence.sqlite import fetch_all

    child = await store.get_session(child_id) if child_id and child_id != session.id else None
    if child is None:
        raise RuntimeError('Autonomous recovery child is missing')
    if child.workspace != workspace:
        raise RuntimeError('Autonomous recovery child workspace identity conflict')
    roots = await fetch_all(
        'SELECT session_id, root_session_id FROM provisional_sessions WHERE session_id IN (?, ?)',
        (session.id, child_id),
    )
    roots = {row['session_id']: row['root_session_id'] for row in roots}
    if child_id in roots and roots[child_id] != roots.get(session.id, session.id):
        raise RuntimeError('Autonomous recovery child root identity conflict')
    resolved = _resolve_session_profile(child, workspace)
    if resolved.snapshot != snapshot:
        raise RuntimeError('Autonomous recovery child profile identity conflict')


@dataclass(frozen=True)
class _Resources:
    turn_engine: LangGraphTurnEngine
    sessions: MemorySessionAdapter
    events: NullEventPublisher


def build_autonomous_session(execution, *, workspace: str, session_id: str,
                             store=None, events=None) -> SessionRunCoordinator:
    store = store if store is not None else ThreadStore()
    runtime = AgentRuntime(_Resources(
        LangGraphTurnEngine(execution), MemorySessionAdapter(), NullEventPublisher(),
    ))
    guidance = GuidanceService(store)
    execution.bind_guidance_service(guidance)
    loop = LoopService(
        store=store, workspace=workspace,
        scheduler=LoopRuntimeScheduler(
            store=store, runtime=runtime, workspace=workspace,
            session_id=session_id, guidance=guidance, events=events,
        ),
    )
    goal = GoalService(
        store=store, workspace=workspace,
        scheduler=GoalRuntimeScheduler(
            store=store, runtime=runtime, workspace=workspace,
            evaluator=GoalEvaluator(), guidance=guidance, events=events,
        ),
    )
    execution.bind_automation_services(loop, goal)
    return SessionRunCoordinator(runtime, loop, goal)


class _SDKLoopRun:
    """Session lifecycle around owned turns; services retain scheduling ownership."""

    def __init__(self, owner, session_id, session_lock, workspace_lock):
        self.owner = owner
        self.session_id = session_id
        self._session_lock = session_lock
        self._workspace_lock = workspace_lock
        self._interactions = {}
        self._driver = None
        self._closing = None
        self.assembly = None
        self.profile = "loop"
        self._completion_task = None
        self._owns_automation = False
        owner.set_cleanup(self._close)

    async def submit_interaction(self, interaction_id, response):
        for coordinator in tuple(self._interactions.values()):
            if await coordinator.submit_interaction(interaction_id, response):
                return True
        return False

    async def cancel(self):
        await self.owner.cancel()

    async def _terminate(self):
        await self.owner._terminate()

    async def _close(self):
        errors = []
        try:
            if self.assembly is not None and self._owns_automation:
                service = (self.assembly.goal_service if self.profile == "goal"
                           else self.assembly.loop_service)
                await service.stop(self.session_id)
        except BaseException as exc:
            errors.append(exc)
        for release in (self._workspace_lock.close, self._session_lock.release):
            try:
                release()
            except BaseException as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup('Autonomous run close failed', errors)

    async def events(self):
        try:
            async for event in self.owner.events():
                yield event
        finally:
            await self._terminate()


async def build_sdk_loop_run(config, settings, api_key, prompt, session, workspace, session_lock,
                             *, execution_factory, publisher_factory):
    import asyncio
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock, OwnedWorkspaceWriteLock
    from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput
    from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
    from voidx.agent.application.runtime.run_ownership import RunOwner
    from voidx.agent.application.runtime.run_supervisor import RunResult
    from voidx.agent.domain.thread import AgentThread
    from voidx.agent.domain.semantic_events import decode_event

    owner = RunOwner()
    locks = OwnedWorkspaceWriteLock(workspace)
    run = _SDKLoopRun(owner, session.id, session_lock, locks)
    run.profile = _resolve_session_profile(session, workspace).runtime_profile.profile_id
    store = ThreadStore()
    guidance = GuidanceService(store)

    from voidx.bootstrap.managed_guidance import current_guidance
    managed_guidance = current_guidance.get()

    class Runtime:
        async def run_turn(self, request):
            if managed_guidance is not None:
                managed_guidance.activate(GuidanceService(store), request.context)
            child_lock = None
            execution = None
            interactions = None
            result = None
            ready = asyncio.get_running_loop().create_future() if owner.in_dispatch() else None

            async def execute(supervisor):
                nonlocal child_lock, execution, interactions, result
                channel = supervisor.channel
                identity = channel.identity
                publisher = publisher_factory(channel)
                interactions = InteractionCoordinator(channel, **identity, budget=owner.interactions)
                run._interactions[identity['turn_id']] = interactions
                if request.thread.session_id != session.id:
                    child_lock = HeadlessFileLock('session', request.thread.session_id)
                    await child_lock.acquire()
                child_session = await store.get_session(request.thread.session_id)
                if child_session is None:
                    raise ValueError('Missing autonomous turn session')
                execution, _ = await execution_factory(
                    config, settings, api_key, child_session, workspace,
                    SemanticOutput(publisher, **identity), interactions,
                    locks.child(identity['turn_id']),
                )
                turn_guidance = GuidanceService(store)
                execution.bind_guidance_service(turn_guidance)
                if managed_guidance is not None and request.thread.session_id == session.id:
                    managed_guidance.activate(turn_guidance, request.context)
                execution.bind_automation_services(loop, goal)
                runtime = AgentRuntime(_Resources(
                    LangGraphTurnEngine(execution), MemorySessionAdapter(), NullEventPublisher(),
                ))
                result = await runtime.run_turn(request)
                if publisher.completed is None:
                    raise RuntimeError('TurnRunner returned without completion')
                return RunResult(completed=publisher.completed)

            async def persist(_):
                await execution.persist_runtime_state()
                if ready is not None:
                    await owner.complete_after_dispatch(supervisor, ready)

            async def cleanup(outcome):
                errors = []
                tail = ()
                try:
                    if interactions is not None:
                        tail = await interactions.cleanup(outcome)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    run._interactions.pop(supervisor.channel.identity['turn_id'], None)
                try:
                    if execution is not None:
                        await execution.aclose()
                except BaseException as exc:
                    errors.append(exc)
                try:
                    if child_lock is not None:
                        child_lock.release()
                except BaseException as exc:
                    errors.append(exc)
                if errors:
                    raise BaseExceptionGroup('Autonomous turn cleanup failed', errors)
                return tail

            def before_cancel():
                if interactions is not None:
                    interactions.begin_cancel()

            supervisor = await owner.register_turn(
                session_id=request.thread.session_id, thread_id=request.thread.thread_id,
                execute=execute, persist=persist, cleanup=cleanup, before_cancel=before_cancel,
            )
            if ready is not None:
                await asyncio.wait((ready, supervisor._task), return_when=asyncio.FIRST_COMPLETED)
                if ready.done():
                    ready.result()
                    return result
            await supervisor.wait()
            terminal = decode_event((await supervisor.channel.completion).terminal)
            if terminal.kind == 'turn.cancelled':
                raise asyncio.CancelledError
            if terminal.kind == 'turn.failed':
                raise RuntimeError('Autonomous turn failed')
            return result

    from voidx.agent.application.runtime.scheduler_events import OwnedSchedulerEvents

    semantic_events = OwnedSchedulerEvents(owner)
    runtime = Runtime()
    from voidx.bootstrap.automation_session_ids import loop_session_id

    loop = LoopService(store=store, workspace=workspace,
        session_id_factory=loop_session_id,
        scheduler=LoopRuntimeScheduler(
        store=store, runtime=runtime, workspace=workspace, session_id=session.id,
        guidance=guidance, owner=owner, semantic_events=semantic_events,
    ))
    goal = GoalService(store=store, workspace=workspace, scheduler=GoalRuntimeScheduler(
        store=store, runtime=runtime, workspace=workspace, evaluator=GoalEvaluator(),
        guidance=guidance, owner=owner, semantic_events=semantic_events,
    ))
    run.assembly = SessionRunCoordinator(runtime, loop, goal)

    async def drive():
        from voidx.agent.domain.thread import TERMINAL_LIFECYCLES

        existing = await store.latest_thread_id_with_prefix(f'{run.profile}:{session.id}:')
        generation = None
        binding = None
        if run.profile == 'goal':
            from voidx.agent.domain.automation.goal import GoalSpecSnapshot, is_goal_terminal
            from voidx.persistence.sqlite import fetch_all

            bindings = await store.list_goal_generations(session.id)
            orphan_inits = await fetch_all(
                """SELECT p.protocol_id FROM goal_protocol_records AS p
                   LEFT JOIN goal_generations AS g ON g.generation = p.generation
                   WHERE p.parent_session_id = ? AND p.phase = 'init'
                     AND g.generation IS NULL ORDER BY p.submitted_at""",
                (session.id,),
            )
            active = []
            for candidate in bindings:
                durable = await store.load(candidate.goal_thread_id) if candidate.goal_thread_id else None
                if candidate.terminal_at is None and (
                        durable is None or not is_goal_terminal(durable.state.lifecycle)):
                    active.append(candidate)
            if len(orphan_inits) + len(active) > 1:
                raise RuntimeError('Ambiguous autonomous recovery generation')
            if orphan_inits:
                init = await store.get_goal_protocol(orphan_inits[0]['protocol_id'])
                snapshot = GoalSpecSnapshot.model_validate(init.payload)
                if (snapshot.parent_thread_id != session.id
                        or snapshot.parent_session_id != session.id
                        or snapshot.generation != init.generation
                        or snapshot.workspace != workspace):
                    raise RuntimeError('Autonomous recovery INIT identity conflict')
                generation = init.generation
                existing = None
            elif bindings:
                binding = active[0] if active else bindings[-1]
                generation = binding.generation
                existing = binding.goal_thread_id
                if not existing:
                    raise RuntimeError('Autonomous recovery thread is missing')
        else:
            from voidx.persistence.sqlite import fetch_all

            candidates = await fetch_all(
                'SELECT id FROM agent_threads WHERE id LIKE ?', (f'loop:{session.id}:%',),
            )
            active = []
            for candidate in candidates:
                durable = await store.load(candidate['id'])
                if durable is None:
                    raise RuntimeError('Autonomous recovery thread is missing')
                if durable.state.lifecycle not in TERMINAL_LIFECYCLES:
                    active.append(candidate['id'])
            if len(active) > 1:
                raise RuntimeError('Ambiguous autonomous recovery generation')
            if active:
                existing = active[0]
        loaded = await store.load(existing) if existing is not None else None
        if existing is not None and loaded is None:
            raise RuntimeError('Autonomous recovery thread is missing')
        recover = generation is not None and loaded is None
        recover = recover or (loaded is not None and (
            not is_goal_terminal(loaded.state.lifecycle) if run.profile == 'goal'
            else loaded.state.lifecycle not in TERMINAL_LIFECYCLES
        ))
        if recover:
            if run.profile == 'goal':
                if binding is not None:
                    if loaded.state.context['goal_spec']['generation'] != generation:
                        raise RuntimeError('Autonomous recovery generation identity conflict')
                    from voidx.agent.domain.agent_profile import AgentProfileSnapshot
                    init_rows = await fetch_all(
                        "SELECT protocol_id FROM goal_protocol_records WHERE generation = ? AND phase = 'init'",
                        (generation,),
                    )
                    if len(init_rows) != 1:
                        raise RuntimeError('Autonomous recovery INIT identity conflict')
                    init = await store.get_goal_protocol(init_rows[0]['protocol_id'])
                    init_snapshot = GoalSpecSnapshot.model_validate(init.payload)
                    expected_profile = (AgentProfileSnapshot.model_validate(init_snapshot.profile_snapshot)
                                        if init_snapshot.profile_snapshot else
                                        _resolve_session_profile(session, workspace).snapshot)
                    for child_id in (binding.work_session_id, binding.evaluator_session_id):
                        await _validate_recovery_child(store, child_id, session, workspace, expected_profile)
                elif loaded is not None:
                    raise RuntimeError('Autonomous recovery binding is missing')
                if managed_guidance is not None and binding is not None:
                    managed_guidance.activate_recovery(guidance, binding)
                status = await goal.resume_generation(generation)
                run._owns_automation = True
                binding = await store.get_goal_generation(generation)
                recovered = await store.load(binding.goal_thread_id) if binding is not None else None
                if recovered is None or recovered.state.lifecycle.value == 'failed':
                    raise RuntimeError('Autonomous recovery failed')
            else:
                child_id = loaded.thread.session_id
                await _validate_recovery_child(
                    store, child_id, session, workspace, _resolve_session_profile(session, workspace).snapshot,
                )
                status = await loop.resume(session.id)
                run._owns_automation = True
        else:
            run._owns_automation = True
            status = await run.assembly.run_idle(run.profile, prompt, AgentThread(
                thread_id=session.id, session_id=session.id, workspace=workspace,
            ))
        if run.profile == 'goal' and status is not None:
            from voidx.agent.domain.automation.goal import is_goal_terminal

            if not is_goal_terminal(status.state):
                await semantic_events.wait_goal_terminal()
        if status is None or run.profile == 'goal':
            # Finish outside the owned driver so draining never waits on itself.
            failed = False
            if run.profile == 'goal' and (recover or status is not None):
                bindings = await store.list_goal_generations(session.id)
                if bindings:
                    durable = await store.load(bindings[-1].goal_thread_id)
                    failed = durable is None or durable.state.lifecycle.value != 'completed'
            run._driver.add_done_callback(lambda task: finish_driver(task, failed=failed))

    def finish_driver(task, *, failed=False):
        if run._closing is None and not task.cancelled() and task.exception() is None:
            owner.request_finish(failed=failed)
            run._completion_task = owner._lifecycle

    run._driver = owner.spawn(drive)
    return run, run
