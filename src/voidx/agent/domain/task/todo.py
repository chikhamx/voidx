"""Todo state value objects owned by the Agent task domain."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class TodoStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"


class TodoRunItem(BaseModel):
    id: str = Field(..., max_length=20, description="Semantic id for the todo item")
    content: str
    status: TodoStatus


class TodoRunState(BaseModel):
    summary: str = ""
    total: int = 0
    done: int = 0
    active: int = 0
    pending: int = 0
    active_items: list[TodoRunItem] = Field(default_factory=list)
    items: list[TodoRunItem] = Field(default_factory=list)
    updated_at: str = ""


__all__ = ["TodoStatus", "TodoRunItem", "TodoRunState"]
