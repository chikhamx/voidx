"""Todo item runtime types shared by tools and UI events."""

from __future__ import annotations

from typing import Literal, TypeAlias

TodoStatus: TypeAlias = Literal["pending", "active", "done"]

__all__ = ["TodoStatus"]
