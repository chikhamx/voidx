"""Default semantic event sink when UI mapping is handled by the execution engine."""

from voidx.agent.domain.events import AgentEvent


class NullEventPublisher:
    def publish(self, event: AgentEvent) -> None:
        pass
