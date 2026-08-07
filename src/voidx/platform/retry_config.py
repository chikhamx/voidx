"""Retry configuration shared by infrastructure clients."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay: float = Field(default=1.0, ge=0.0, le=60.0)
    max_delay: float = Field(default=10.0, ge=0.0, le=120.0)
    jitter: bool = Field(default=True)


__all__ = ["RetryConfig"]
