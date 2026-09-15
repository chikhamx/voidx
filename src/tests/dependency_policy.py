"""Approved exact dependency exceptions shared by boundary tests."""

AGENT_APPLICATION_DTO_DEPENDENCIES = frozenset({
    (
        "voidx.agent.application.runtime.interaction_coordinator",
        "voidx.tooling.domain.interaction",
    ),
})
