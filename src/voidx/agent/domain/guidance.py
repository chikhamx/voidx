"""Cross-mode guidance submitted to a durable runtime inbox."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


GuidanceSource = Literal["user", "system", "guard"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Guidance(BaseModel):
    """A user or runtime instruction waiting for a matching delivery."""

    model_config = ConfigDict(frozen=True)

    guidance_id: str
    text: str
    truncated: bool = False
    source: GuidanceSource = "user"
    created_at: datetime = Field(default_factory=_now)
    target_session_id: str | None = None
    target_thread_id: str | None = None
    target_run_id: str | None = None
    target_phase: str | None = None
    delivery_id: str | None = None
    delivered_phase: str | None = None
    consumed_at: datetime | None = None

    @field_validator(
        "guidance_id",
        "text",
        "target_session_id",
        "target_thread_id",
        "target_run_id",
        "target_phase",
        "delivery_id",
        "delivered_phase",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None
