"""Persistence boundary for durable filesystem grants."""

from __future__ import annotations

from typing import Protocol

from voidx.tooling.domain.grants import GrantDelta


class GrantRepository(Protocol):
    """Persist a grant delta without owning authorization policy."""

    def __call__(self, delta: GrantDelta) -> object:
        """Merge a delta into the durable grant representation."""
