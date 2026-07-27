"""Runtime-backed execution adapter for scheduled /loop prompts."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.domain.loop import LOOP_PROFILE, LoopSpec, LoopToolView
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime.contracts import TurnRequest
from voidx.agent.runtime.dispatcher import DispatchResult, RuntimeDispatcher
from voidx.memory.thread_store import ThreadStore
from voidx.agent.loop.controller import LoopAttemptController



@dataclass(frozen=True)
class LoopRuntimeRunner:
    runtime: object

    async def run_turn(self, *, thread, profile, input_frame: dict) -> RuntimeDecision:
        prompt = str(input_frame.get("prompt", ""))
        if not prompt.strip():
            return RuntimeDecision(
                outcome="failed",
                summary="Loop prompt was empty.",
                reason="empty_loop_prompt",
            )
        spec = LoopSpec.model_validate(input_frame.get("spec") or {"prompt": prompt})
        controller = LoopAttemptController(spec=spec)
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=profile,
            workspace=thread.workspace,
            tool_policy=LoopToolView.default(workflow_enabled=False).bind(_available_loop_tool_ids()),
            loop_controller=controller,
        )
        await self.runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=prompt,
                display_text=str(input_frame.get("display_text") or "") or None,
                context=context,
                runtime=None,
            )
        )
        submitted = controller.final_decision()
        if submitted is not None:
            return submitted
        return RuntimeDecision(
            outcome="completed",
            summary="Loop turn completed.",
            progress="meaningful",
        )


class LoopRuntimeScheduler:
    def __init__(
        self,
        *,
        store: ThreadStore,
        runtime,
        workspace: str,
        lease_owner: str = "loop-manager",
        lease_seconds: float = 60,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._workspace = workspace
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds

    async def run_prompt(
        self,
        prompt: str,
        *,
        display_text: str | None,
        session_id: str | None,
        spec: LoopSpec | None = None,
    ) -> DispatchResult | None:
        loop_spec = spec or LoopSpec(prompt=prompt)
        loaded = await self._ensure_thread(session_id, loop_spec)
        outbox = await self._store.enqueue_outbox(
            thread_id=loaded.thread.thread_id,
            kind="loop_prompt",
            payload={
                "prompt": prompt,
                "display_text": display_text,
                "spec": loop_spec.model_dump(mode="json"),
            },
            expected_state_version=loaded.state_version,
        )
        dispatcher = RuntimeDispatcher(
            store=self._store,
            runner=LoopRuntimeRunner(self._runtime),
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        return await dispatcher.dispatch_outbox(outbox.outbox_id)

    async def _ensure_thread(self, session_id: str | None, spec: LoopSpec):
        thread_id = spec.loop_thread_id(session_id)
        loaded = await self._store.load(thread_id)
        if loaded is not None:
            return loaded
        return await self._store.create_thread(
            AgentThread(
                thread_id=thread_id,
                session_id=session_id,
                parent_thread_id=session_id,
                workspace=self._workspace,
            ),
            profile=LOOP_PROFILE,
            state=AgentThreadState(thread_id=thread_id, lifecycle=LifecycleState.READY),
            resource_scope={"workspace": self._workspace},
        )


def _available_loop_tool_ids() -> set[str]:
    return {
        "loop_update",
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "websearch",
        "webfetch",
        "mcp",
        "skill",
        "workflow",
        "task_status",
        "todo",
        "schedule_wakeup",
        "clarify",
        "checkpoint",
        "agent",
        "bash",
        "write",
    }

def _loop_thread_id(session_id: str | None) -> str:
    return f"loop:{session_id or 'default'}"
