"""Committed scheduler state on the dispatch's still-open child channel."""
import asyncio
import json
import time
from uuid import uuid4

from voidx.agent.domain import semantic_events as e


class OwnedSchedulerEvents:
    def __init__(self, owner):
        self._owner = owner
        self._goal_terminal = asyncio.Event()
        self._goal_lifecycle = None

    async def wait_goal_terminal(self) -> str:
        """Observe this owned run's sticky, post-commit terminal notification."""
        await self._goal_terminal.wait()
        return self._goal_lifecycle

    async def committed(self, *, decision=None, goal_phase=None, available_at=None, lifecycle=None) -> None:
        channel = self._owner.dispatch_channel()
        # Pre-execution validation has no child identity and must not invent a turn.
        if channel is None:
            if lifecycle in ('completed', 'blocked', 'failed', 'cancelled'):
                self._goal_lifecycle = lifecycle
                self._goal_terminal.set()
            elif goal_phase is not None and goal_phase.needs_resume:
                raise RuntimeError(f'Goal requires resume before child turn: {goal_phase.reason}')
            return

        async def emit(event_type, payload):
            try:
                await channel.publish(event_type(
                    **channel.identity, agent_id=None, parent_tool_call_id=None,
                    event_id=uuid4(), sequence=1, timestamp=time.time(), payload=payload,
                ))
            except RuntimeError as exc:
                if str(exc) == 'Run is not accepting events' and not self._owner.accepting_dispatches:
                    raise asyncio.CancelledError from None
                raise

        if decision is not None:
            waiting = decision.outcome == 'continue'
            description_data = decision.model_dump(mode='json')
            if waiting:
                description_data['available_at'] = available_at
            description = json.dumps(description_data)
            payload = dict(status_id='loop:waiting', stage='waiting' if waiting else 'idle',
                           description=description, parent_tool_call_id=None)
            if waiting:
                await emit(e.StatusUpdated, e.StatusPayload(**payload))
            else:
                await emit(e.StatusFinished, e.StatusFinishedPayload(
                    **payload, ok=decision.outcome not in ('failed', 'needs_user'), result=decision.summary))
        else:
            payload = dict(
                status_id='goal:phase', stage='needs_resume' if goal_phase.needs_resume else goal_phase.phase,
                description=json.dumps(goal_phase.model_dump(mode='json')), parent_tool_call_id=None,
            )
            if lifecycle in ('completed', 'blocked', 'failed', 'cancelled'):
                await emit(e.StatusFinished, e.StatusFinishedPayload(
                    **payload, ok=lifecycle == 'completed', result=goal_phase.reason))
            else:
                await emit(e.StatusUpdated, e.StatusPayload(**payload))
            if goal_phase.reason:
                await emit(e.DiagnosticWarning, e.DiagnosticPayload(
                    code='goal_needs_resume' if goal_phase.needs_resume else goal_phase.reason,
                    summary=goal_phase.reason, recoverable=True,
                ))
            if lifecycle in ('completed', 'blocked', 'failed', 'cancelled'):
                # Do not let run completion overtake backpressured status output.
                self._goal_lifecycle = lifecycle
                self._goal_terminal.set()
