"""Explicit sequential-run SDK/Gateway composition; never installed by default."""
from contextlib import aclosing
from collections.abc import AsyncIterator
from typing import Protocol, Any
import inspect
import asyncio

from voidx.agent.domain.semantic_events import SemanticEvent
from voidx.tooling.domain.interaction import InteractionResponse
from voidx.presentation.output.events.schema import UiEvent, CaptureStarted, CaptureStopped
from voidx.presentation.adapters.semantic_event_projector import SemanticEventProjector
from voidx.presentation.gateway.semantic_interactions import SemanticInteractionRouter
from voidx.presentation.gateway.session.core import GatewaySession
from voidx.presentation.output.tree import OutputTree
from voidx.presentation.adapters.persistence.transcript_snapshot import (
    append_transcript_turns, tree_transcript_turn_ids, tree_to_transcript_turn_rows,
    transcript_rows_to_tree, complete_transcript_turn_ids, load_transcript,
)

from voidx.agent.ports.persistence import SessionRepository
from voidx.agent.adapters.persistence.session_adapter import SessionRepositoryAdapter


class AgentPort(Protocol):
    def stream(self, prompt: str, *, session_id: str = "", workspace: str = ".") -> AsyncIterator[SemanticEvent]: ...
    async def submit_interaction(self, request_id: str, response: InteractionResponse) -> bool: ...
    async def cancel(self) -> None: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> Any: ...


class DisplayConsumer(Protocol):
    def handle(self, event: UiEvent) -> Any: ...
    async def drain_stream_commits(self) -> None: ...



class SemanticGatewayBridge:
    def __init__(self, agent: AgentPort, *, consumer: DisplayConsumer,
                 tree: OutputTree, workspace: str = ".", manage_capture: bool = True,
                 session_repository: SessionRepository | None = None,
                 session: GatewaySession | None = None) -> None:
        """Own the supplied consumer capture exclusively during each run by default.

        Set manage_capture=False when capture is already active and externally owned.
        Capture is data-only; the bridge never activates a Live display.
        """
        self._workspace = workspace
        self._sessions = session_repository or SessionRepositoryAdapter()
        self._next_display_turn = 0
        self._manage_capture = manage_capture
        self.agent = agent
        self.consumer = consumer
        self._active = False
        self._run_task = None
        self.interactions = SemanticInteractionRouter(self.agent.submit_interaction)
        self.projector = SemanticEventProjector()
        self.tree = tree
        self._session_bindings: dict[str, str] = {}
        self._turn_bindings: dict[tuple[str, str, str], int] = {}
        self._shared_session = session is not None
        self.session = session or GatewaySession(lambda: self.tree,
            interaction_router=self.interactions, interaction_cancel=self.agent.cancel,
            command_handler=self._command,
            workspace=workspace,
            transcript_session_resolver=lambda tid: self._session_bindings.get(tid, ""),
            thread_tree_provider=self._thread_tree)
        self._thread_id = ""

    async def __aenter__(self):
        self._session_bindings.update(await self._sessions.semantic_thread_bindings(self._workspace))
        for tid, sid in self._session_bindings.items():
            await self.session.register_thread(tid)
            await self._restore_display_allocators(sid)
        await self.agent.__aenter__()
        return self

    async def _restore_display_allocators(self, session_id: str) -> None:
        ids = await complete_transcript_turn_ids(session_id)
        self._next_display_turn = max(self._next_display_turn, max(ids, default=-1) + 1)
        for row in await load_transcript(session_id):
            tree_id = row.metadata.get("tree_id")
            if isinstance(tree_id, str):
                self.tree._sync_counter(tree_id)

    async def __aexit__(self, exc_type, exc, tb):
        task = self._run_task
        failures = []
        try:
            try:
                await self.agent.aclose()
            except BaseException as error:
                failures.append(error)
            if task is not None and task is not asyncio.current_task():
                try:
                    await asyncio.shield(task)
                except BaseException as error:
                    failures.append(error)
        finally:
            self.interactions.close()
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Bridge close and run failed", failures)

    async def _command(self, command):
        if command.kind != "cancel":
            raise ValueError("Use the explicitly owned bridge.run for submission")
        if command.thread_id != self._thread_id or not self._thread_id:
            raise ValueError("Cancellation thread does not match the active run")
        await self.agent.cancel()
        return True

    async def run(self, prompt: str, *, session_id: str = "", workspace: str = ".", observer=None) -> None:
        if self._active:
            raise RuntimeError("Only one active bridge run is allowed")
        self.interactions = SemanticInteractionRouter(self.agent.submit_interaction)
        if not self._shared_session:
            self.session._interaction_router = self.interactions
        self.projector = SemanticEventProjector()
        self._active = True
        self._thread_id = session_id
        self._run_task = asyncio.current_task()
        failures = []
        try:
            if self._manage_capture:
                await self._handle(CaptureStarted())
            async with aclosing(self.agent.stream(prompt, session_id=session_id, workspace=workspace)) as events:
                async for event in events:
                    if not self._thread_id:
                        self._thread_id = event.thread_id
                    if event.thread_id not in self._session_bindings:
                        await self._restore_display_allocators(event.session_id)
                        if self._shared_session and event.thread_id != self._thread_id:
                            from voidx.agent.adapters.persistence.thread_repository import ThreadStore

                            store = ThreadStore()
                            loaded = await store.load(event.thread_id)
                            info = await self._sessions.get_session(event.session_id)
                            if info is None:
                                raise ValueError("SDK child has no durable session binding")
                            if loaded is not None:
                                if loaded.thread.session_id != event.session_id:
                                    raise ValueError("SDK thread does not match durable session identity")
                                child_workspace = loaded.thread.workspace
                            else:
                                # Evaluator turns have a durable generation/session binding, not an agent_threads row.
                                generations = await store.list_goal_generations(self._thread_id)
                                if not any(
                                    g.evaluator_session_id == event.session_id for g in generations
                                ):
                                    raise ValueError("SDK child has no durable session binding")
                                child_workspace = info.workspace
                            if event.thread_id not in {row.thread_id for row in self.session.list_threads()}:
                                await self.session.register_thread(
                                    event.thread_id,
                                    workspace=child_workspace,
                                    title=info.title or "Child session",
                                    directory=info.directory,
                                    runtime_profile=info.runtime_profile,
                                )
                    previous = self._session_bindings.setdefault(event.thread_id, event.session_id)
                    if previous != event.session_id:
                        raise ValueError("SDK thread changed durable session identity")
                    if observer is not None:
                        observer(event)
                    # Cancellation can seal a supervisor before TurnRunner starts.
                    if event.sequence == 1 and event.kind in {"turn.cancelled", "turn.failed"}:
                        from voidx.presentation.output.events.schema import TurnCancelled, TurnFailed
                        terminal = (TurnCancelled(thread_id=event.thread_id)
                            if event.kind == "turn.cancelled" else
                            TurnFailed(thread_id=event.thread_id, message=str(event.payload)))
                        await self.session.broadcast_event(terminal, thread_id=event.thread_id)
                        continue
                    result = self.projector.project_result(event,
                        raw_text=prompt if event.kind == "turn.started"
                        and event.thread_id == self._thread_id else None)
                    for projected in result.events:
                        await self._handle(projected)
                    await self.consumer.drain_stream_commits()
                    key = (event.session_id, event.thread_id, event.turn_id)
                    if event.kind == "turn.started":
                        allocated = tree_transcript_turn_ids(self.tree)[-1]
                        turn_id = max(allocated, self._next_display_turn)
                        for node in self.tree.root.children:
                            if node.node_type == "turn" and node.payload.get("transcript_turn_id") == allocated:
                                node.payload["transcript_turn_id"] = turn_id
                        self._next_display_turn = turn_id + 1
                        self._turn_bindings[key] = turn_id
                    if event.kind in {"turn.completed", "turn.cancelled", "turn.failed"}:
                        turn_id = self._turn_bindings[key]
                        rows = tree_to_transcript_turn_rows(
                            event.session_id, self.tree, turn_id, include_separators=True)
                        for row in rows:
                            row.metadata["semantic_identity"] = {
                                "session_id": event.session_id, "thread_id": event.thread_id}
                        await append_transcript_turns(event.session_id, [(turn_id, rows)])
                    if result.interaction is not None:
                        self.interactions.register(result.interaction, result.request)
                        # The client opens both business prompts and ui.request as dialogs.
                        await self.session.publish_interaction(result.request,
                            sync_snapshot=result.interaction.purpose == "checkpoint")
                    elif result.resolved is not None:
                        self.interactions.resolve(result.resolved.payload.interaction_id,
                            session_id=event.session_id, thread_id=event.thread_id, turn_id=event.turn_id)
                        if any(projected.kind == "checkpoint_decision.submitted" for projected in result.events):
                            await self.session.broadcast_snapshot(sync_persisted=False, force_snapshot=True)
                        else:
                            for projected in result.events:
                                await self.session.broadcast_event(projected, thread_id=event.thread_id)
                    else:
                        for projected in result.events:
                            await self.session.broadcast_event(projected, thread_id=event.thread_id)
        except BaseException as error:
            failures.append(error)
        finally:
            try:
                await self.consumer.drain_stream_commits()
            except BaseException as error:
                failures.append(error)
            if self._manage_capture:
                try:
                    await self._handle(CaptureStopped())
                except BaseException as error:
                    failures.append(error)
            self._run_task = None
            self._active = False
            self._thread_id = ""
            self.interactions.close()

        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Bridge run and capture cleanup failed", failures)

    def _thread_tree(self, thread_id: str) -> OutputTree | None:
        if not self._active:
            return None
        rows = []
        for (session_id, tid, _), turn_id in self._turn_bindings.items():
            if tid == thread_id:
                rows.extend(tree_to_transcript_turn_rows(session_id, self.tree, turn_id, include_separators=True))
        return transcript_rows_to_tree(rows, preserve_tree_ids=True)

    async def _handle(self, event: UiEvent) -> None:
        handled = self.consumer.handle(event)
        if inspect.isawaitable(handled):
            await handled
