"""Managed SDK owners attached to the production Gateway, not a second server."""
import asyncio

from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
from voidx.bootstrap.managed_guidance import ManagedGuidance, current_guidance
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.protocol.v2.methods import MethodParamsError
from voidx.sdk import VoidxAgent


class ProductionSdkGateway:
    def __init__(self, session, legacy_handler, *, config, settings):
        self.session = session
        self.legacy_handler = legacy_handler
        self.config = config
        self.settings = settings
        self.owners = {}
        self._guidance = {}
        self._session_bindings = {}
        self.tasks = {}
        self.closed = False
        self.legacy_pending = 0
        session._interaction_router = self
        session._interaction_cancel = self.cancel_all
        session._thread_tree_provider = self.thread_tree
        session._transcript_session_resolver = self.session_id
        session.set_command_handler(self.handle)

    def owns_transcript(self, thread_id):
        return thread_id in self._session_bindings or any(
            thread_id in bridge._session_bindings for bridge in self.owners.values()
        )

    def session_id(self, thread_id):
        for bridge in self.owners.values():
            if thread_id in bridge._session_bindings:
                return bridge._session_bindings[thread_id]
        if thread_id in self._session_bindings:
            return self._session_bindings[thread_id]
        return self._legacy_session_id(thread_id)

    async def _legacy_session_id(self, thread_id):
        from voidx.agent.adapters.persistence.thread_repository import ThreadStore

        loaded = await ThreadStore().load(thread_id)
        if loaded is not None:
            return loaded.thread.session_id
        return thread_id

    async def restore(self):
        from voidx.agent.adapters.persistence.session_adapter import SessionRepositoryAdapter
        self._session_bindings.update(
            await SessionRepositoryAdapter().semantic_thread_bindings(self.config.workspace))
        for thread_id in self._session_bindings:
            await self.session.register_thread(thread_id)

    def thread_tree(self, thread_id):
        for bridge in self.owners.values():
            tree = bridge._thread_tree(thread_id)
            if tree is not None and tree.root.children:
                return tree
        return None

    def requests(self):
        return tuple(r for b in self.owners.values() for r in b.interactions.requests())

    async def respond(self, request_id, value, *, thread_id):
        for bridge in tuple(self.owners.values()):
            if await bridge.interactions.respond(request_id, value, thread_id=thread_id):
                return True
        if self.legacy_pending and thread_id:
            from voidx.presentation.protocol import UiResponse
            return self.session._run_manager.resolve_pending_request(
                thread_id, UiResponse(request_id=request_id, value=value))
        return False

    def close(self):
        for bridge in self.owners.values():
            bridge.interactions.close()

    async def handle(self, command):
        kind = command.get("kind") if isinstance(command, dict) else command.kind
        root = command.get("thread_id", "") if isinstance(command, dict) else command.thread_id
        if kind == "cancel" and root in self.tasks:
            bridge = self.owners.get(root)
            if bridge is not None:
                await bridge.agent.cancel()
            task = self.tasks.get(root)
            if task is not None:
                await task
            return
        if kind == "guide" and root not in self.tasks:
            return await self.legacy_handler(command)
        if kind == "guide":
            await self._guidance[root].submit(command["text"])
            return
        if kind != "submit" or command.text.lstrip().startswith("/"):
            if self.tasks:
                raise MethodParamsError("Legacy commands require SDK runs to finish or be cancelled")
            if kind == "submit":
                self.legacy_pending += 1
                try:
                    return await self.legacy_handler(command)
                except BaseException:
                    self.legacy_pending -= 1
                    raise
            return await self.legacy_handler(command)
        if self.legacy_pending:
            raise MethodParamsError("Legacy command is queued or running; wait before starting an SDK run")
        if self.closed:
            raise RuntimeError("SDK Gateway is closed")
        dock = BottomInputDock()
        bridge = SemanticGatewayBridge(VoidxAgent(self.config, settings=self.settings),
            consumer=DockEventConsumer(dock), tree=dock.tree,
            workspace=command.workspace, session=self.session)
        self.owners[root] = bridge
        self._guidance[root] = ManagedGuidance()
        self.session._managed_run_threads.add(root)
        task = asyncio.create_task(self._run(root, bridge, command))
        self.tasks[root] = task
        self.session._run_manager.actor(root).state.task = task

    async def _run(self, root, bridge, command):
        manager = self.session._run_manager
        failures = []
        capability = self._guidance[root]
        token = current_guidance.set(capability)
        try:
            try:
                await bridge.run(command.text, session_id=root, workspace=command.workspace)
            except BaseException as error:
                failures.append(error)
            try:
                await bridge.agent.aclose()
            except BaseException as error:
                failures.append(error)
            self._session_bindings.update(bridge._session_bindings)
            bridge.interactions.close()
            # Presentation is terminal; the actor still reserves admission until send cleanup.
            self.session._managed_terminal_statuses[root] = "failed" if failures else "idle"
            try:
                self.session._sync_thread_status(root)
                await self.session.broadcast_snapshot(sync_persisted=False)
            except BaseException as error:
                failures.append(error)
        finally:
            capability.close()
            current_guidance.reset(token)
            if self._guidance.get(root) is capability:
                self._guidance.pop(root)
            if self.owners.get(root) is bridge:
                self.owners.pop(root)
            if self.tasks.get(root) is asyncio.current_task():
                self.tasks.pop(root)
                self.session._managed_run_threads.discard(root)
                self.session._managed_terminal_statuses.pop(root, None)
                # Admission stays reserved through the last owner-originated send.
                if failures:
                    manager.fail_turn(root, str(failures[0]))
                else:
                    manager.complete_turn(root)
                self.session._sync_thread_status(root)
        if failures:
            error = BaseExceptionGroup("SDK owner run and cleanup failed", failures)
            from voidx.observability import log_internal_error
            log_internal_error(error, context="production_sdk_gateway.run", session_id=root)
            raise error

    def legacy_completed(self):
        self.legacy_pending = max(0, self.legacy_pending - 1)

    async def cancel_all(self):
        tasks = tuple(self.tasks.values())
        results = await asyncio.gather(
            *(b.agent.cancel() for b in tuple(self.owners.values())),
            return_exceptions=True)
        # Owner-originated sends may fail concurrently; joining peer owners here
        # would create a mutual wait even when the current task is excluded.
        if asyncio.current_task() not in tasks:
            results.extend(await asyncio.gather(*tasks, return_exceptions=True))
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("SDK owner cancellation failed", failures)

    async def aclose(self):
        self.closed = True
        try:
            await self.cancel_all()
        finally:
            self.close()
