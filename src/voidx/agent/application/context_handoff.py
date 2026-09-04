"""Explicit, auditable context handoff from a parent agent to a child run.

The parent passes only what the delegated scope needs: project instructions
(with provenance), selected profile sections, and a task-relevant summary.
The parent transcript and parent-private workflow state never cross over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voidx.agent.domain.prompt_contracts import ContextSection


@dataclass(frozen=True)
class ChildContextHandoff:
    """Trimmed parent context explicitly handed to one child run."""

    instructions: tuple[str, ...] = ()
    profile_sections: tuple[ContextSection, ...] = ()
    summary: str = ""
    source_paths: tuple[str, ...] = ()
