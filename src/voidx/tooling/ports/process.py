"""Process sandbox port used by shell policy adapters."""

from __future__ import annotations

from typing import Protocol


class ProcessSandbox(Protocol):
    def usable_for(self, shell: str) -> bool: ...


__all__ = ["ProcessSandbox"]
