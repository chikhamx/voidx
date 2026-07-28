"""Public agent application API.

Imports are lazy so that submodules (e.g. ``voidx.agent.domain.*``) can be
imported from lower layers without pulling in the full composition chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.agent.composition import build_agent_app
    from voidx.agent.facade import AgentFacade, RunLoopStartupError

__all__ = ["AgentFacade", "RunLoopStartupError", "build_agent_app"]


def __getattr__(name: str):
    if name == "build_agent_app":
        from voidx.agent.composition import build_agent_app

        return build_agent_app
    if name in ("AgentFacade", "RunLoopStartupError"):
        from voidx.agent import facade

        return getattr(facade, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
