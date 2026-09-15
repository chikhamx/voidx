"""Shared storage identity factories for production automation composition."""
from hashlib import sha256

from voidx.agent.domain.automation.loop import LoopSpec


def loop_session_id(spec: LoopSpec, parent: str | None) -> str:
    return 'loop_' + sha256(spec.loop_thread_id(parent).encode()).hexdigest()
