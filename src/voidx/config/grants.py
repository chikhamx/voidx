"""Persistent grant persistence shape owned by config.

The permission layer consumes this DTO; config owns it because it mirrors the
four persistent grant lists stored in workspace settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GrantDelta:
    readable_files: list[str] = field(default_factory=list)
    readable_dirs: list[str] = field(default_factory=list)
    writable_files: list[str] = field(default_factory=list)
    writable_dirs: list[str] = field(default_factory=list)
