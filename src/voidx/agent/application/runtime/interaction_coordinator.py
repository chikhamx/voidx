"""Run-owned decisions with bounded, cancellation-safe event publication."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

from voidx.agent.domain import semantic_events as e
from voidx.agent.ports.events import SemanticEventPublisher
from voidx.agent.ports.run_lifecycle import InteractionBudget
from voidx.tooling.domain.interaction import (
    InteractionRequest, InteractionResolution, InteractionResponse,
)


@dataclass
class _Pending:
    request: InteractionRequest
    future: asyncio.Future[InteractionResolution] | None
    resolution: InteractionResolution | None = None
    required: bool = False


class InteractionCoordinator:
    def __init__(self, publisher: SemanticEventPublisher, *, session_id: str,
                 thread_id: str, turn_id: str, legacy_auto_approve: bool = False,
                 budget: InteractionBudget | None = None) -> None:
        self._budget = budget
        self._held_tokens = 0
        cleanups = getattr(publisher, "_interaction_cleanups", None)
        if cleanups is not None:
            cleanups.append(self._discard)
        self._token = str(uuid4())
        self._issued = 0
        self._next_id: str | None = None
        self._publisher = publisher
        self._identity = dict(session_id=session_id, thread_id=thread_id, turn_id=turn_id)
        self._legacy = legacy_auto_approve
        self._pending: dict[str, _Pending] = {}
        self._used: set[str] = set()
        self._checkpoint_scopes: set[str] = set()
        self._cancelling = False

    def _release_token(self) -> None:
        self._held_tokens -= 1
        self._budget.release()

    def _discard(self) -> None:
        self.begin_cancel()
        self._pending.clear()
        while self._held_tokens:
            self._release_token()

    def issue_id(self) -> str:
        if self._next_id is not None:
            raise RuntimeError("Previous interaction capability has not been consumed")
        if self._cancelling or (self._budget is not None and self._budget.closed):
            raise RuntimeError("Run is not accepting interactions")
        self._issued += 1
        self._next_id = f"{self._token}:{self._issued}"
        return self._next_id

    def _ownership(self, value: InteractionRequest | InteractionResponse) -> None:
        if any(getattr(value, key) != expected for key, expected in self._identity.items()):
            raise ValueError("Interaction ownership does not match this run")

    def _envelope(self) -> dict:
        return dict(**self._identity, event_id=uuid4(), sequence=1, timestamp=time.time(),
                    agent_id=None, parent_tool_call_id=None)

    def _fallback(self, request: InteractionRequest, reason: str) -> InteractionResolution:
        purpose = request.purpose
        decision = {"permission": "deny", "clarify": "skipped", "generic": "cancelled"}.get(purpose, "rejected")
        if purpose == "generic" and reason == "user_rejected":
            decision = "rejected"
        if self._legacy and purpose in {"goal", "loop"} and reason in {"timed_out", "dismissed"}:
            decision = "auto_approved"
        return InteractionResolution(decision=decision, resolution_reason=reason)

    def _decide(self, pending: _Pending, resolution: InteractionResolution) -> bool:
        if pending.resolution is not None:
            return False
        pending.resolution = resolution
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(resolution)
        return True

    def begin_cancel(self) -> None:
        """Synchronously seal decisions before the supervisor cancels producers."""
        self._cancelling = True
        if self._budget is not None:
            for _ in self._checkpoint_scopes:
                self._release_token()
        self._checkpoint_scopes.clear()
        for pending in self._pending.values():
            self._decide(pending, self._fallback(pending.request, "task_cancelled"))

    def _resolved(self, pending: _Pending) -> e.InteractionResolved:
        assert pending.resolution is not None
        return e.InteractionResolved(**self._envelope(), payload=e.InteractionResolvedPayload(
            interaction_id=pending.request.interaction_id, purpose=pending.request.purpose,
            resolution=pending.resolution,
        ))

    async def request(self, request: InteractionRequest) -> InteractionResolution:
        if request.checkpoint_stage is not None:
            request = InteractionRequest.model_validate(request.model_dump())
        self._ownership(request)
        if self._cancelling or (self._budget is not None and self._budget.closed):
            raise RuntimeError("Run is not accepting interactions")
        key = request.interaction_id
        if key in self._used:
            raise ValueError("Interaction ID cannot be reused within a run")
        if len(self._pending) >= 64:
            raise RuntimeError("Run has reached the 64 unfinished interaction limit")
        if request.checkpoint_stage == "scope" and request.checkpoint_id not in self._checkpoint_scopes:
            raise ValueError("Checkpoint scope requires a resolved modified choice")
        if request.checkpoint_stage == "decision" and len(self._checkpoint_scopes) + len(self._pending) >= 64:
            raise RuntimeError("Run has reached the 64 checkpoint association limit")
        if self._budget is not None:
            if key != self._next_id:
                raise ValueError("Interaction requires a fresh issued capability")
            self._next_id = None
            if request.checkpoint_stage != "scope":
                await self._budget.acquire()
                self._held_tokens += 1
        request = request.model_copy(deep=True)
        if request.checkpoint_stage == "scope":
            self._checkpoint_scopes.remove(request.checkpoint_id)
        future = asyncio.get_running_loop().create_future()
        pending = _Pending(request, future)
        if self._budget is None:
            self._used.add(key)
        self._pending[key] = pending
        timer = None
        published = False
        try:
            await self._publisher.publish(e.InteractionRequired(**self._envelope(),
                payload=e.InteractionRequiredPayload(request=request)))
            pending.required = True
            timer = asyncio.get_running_loop().call_later(
                request.timeout, self._decide, pending, self._fallback(request, "timed_out"))
            resolution = await asyncio.shield(future)
            if self._cancelling or (self._budget is not None and self._budget.closed):
                raise asyncio.CancelledError
            await self._publisher.publish(self._resolved(pending))
            published = True
            if (request.checkpoint_stage == "decision" and resolution.decision == "modified"
                    and resolution.value == "modified" and not resolution.free_text
                    and resolution.resolution_reason == "answered"):
                self._checkpoint_scopes.add(request.checkpoint_id)
            return resolution
        except asyncio.CancelledError:
            self._decide(pending, self._fallback(request, "task_cancelled"))
            raise
        finally:
            if timer is not None:
                timer.cancel()
            if not future.done():
                future.cancel()
            pending.future = None
            if published or not pending.required:
                del self._pending[key]
                if self._budget is not None and request.checkpoint_id not in self._checkpoint_scopes:
                    self._release_token()

    async def submit_interaction(self, interaction_id: str, response: InteractionResponse) -> bool:
        """Validate and wake the request only; never wait for event publication."""
        pending = self._pending.get(interaction_id)
        if (self._cancelling or (self._budget is not None and self._budget.closed)
                or pending is None or pending.resolution is not None):
            return False
        self._ownership(response)
        request = pending.request
        if response.scope is not None:
            if request.input_kind != "permission" or response.scope not in request.allowed_scopes:
                raise ValueError("Invalid permission scope")
            if any(tool.allowed_scopes and response.scope not in tool.allowed_scopes for tool in request.tools):
                raise ValueError("Scope is not allowed for every requested tool")
        if response.cancelled or response.rejected:
            resolution = self._fallback(request, "user_rejected" if response.rejected else "dismissed")
        else:
            if response.free_text:
                if request.input_kind != "text" and not request.allow_free_text:
                    raise ValueError("Free text is not allowed")
            elif request.input_kind != "text" and response.value not in {c.value for c in request.choices}:
                raise ValueError("Unknown interaction choice")
            negative = request.purpose != "clarify" and not response.free_text and response.value.lower() in {"no", "deny", "reject", "rejected"}
            resolution = InteractionResolution(value=response.value, free_text=response.free_text,
                scope=response.scope, decision="approved" if request.purpose in {"permission", "checkpoint", "goal", "loop"} else "answered",
                resolution_reason="answered")
            if request.purpose in {"goal", "loop"}:
                if response.free_text:
                    resolution.decision = "revised"
                elif response.value in {"approved", "revised", "cancelled"}:
                    resolution.decision = response.value
                else:
                    resolution.decision = "rejected"
            if request.purpose == "checkpoint":
                if request.checkpoint_stage == "scope" or response.free_text:
                    resolution.decision = "modified"
                    negative = False
                elif response.value in {"approved", "needs_doc", "modified", "rejected"}:
                    resolution.decision = response.value
            if negative:
                fallback = self._fallback(request, "user_rejected")
                resolution.decision = fallback.decision
                resolution.resolution_reason = fallback.resolution_reason
        return self._decide(pending, resolution)

    async def cleanup(self, outcome: str) -> tuple[e.InteractionResolved, ...]:
        """Called only after producers stop; never writes to the business queue."""
        self.begin_cancel()
        tail = tuple(self._resolved(pending) for pending in self._pending.values() if pending.required)
        self._pending.clear()
        return tail
