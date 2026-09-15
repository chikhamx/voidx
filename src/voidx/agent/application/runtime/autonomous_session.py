"""Session-scoped entry points sharing the production automation runtime."""
from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.application.automation.goal.goal_idle import GoalIdleTurnService
from voidx.agent.application.automation.loop.loop_idle import LoopIdleTurnService
from voidx.agent.application.runtime.runtime import AgentRuntime


@dataclass(frozen=True)
class SessionRunCoordinator:
    runtime: AgentRuntime
    loop_service: object
    goal_service: object

    async def run_idle(self, profile: str, prompt: str, thread):
        if profile == 'loop':
            idle = LoopIdleTurnService(self.runtime, self.loop_service)
        elif profile == 'goal':
            idle = GoalIdleTurnService(self.runtime, self.goal_service)
        else:
            raise ValueError(f'Unsupported autonomous profile: {profile}')
        return await idle.run(prompt, thread, parent_thread_id=thread.thread_id)
