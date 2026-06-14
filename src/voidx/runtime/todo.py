"""Todo item runtime types shared by tools and UI events."""

from __future__ import annotations

from typing import Literal, TypeAlias

TodoStatus: TypeAlias = Literal["pending", "in_progress", "completed", "cancelled"]

__all__ = ["TodoStatus"]
