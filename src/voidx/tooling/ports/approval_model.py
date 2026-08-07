"""Narrow model capability used by AI approval application logic."""

from __future__ import annotations

from typing import Protocol, Sequence

from voidx.tooling.domain.ai_approval import AiApprovalRequestItem, AiApprovalResponse


class ApprovalModel(Protocol):
    """Invoke a configured structured-output reviewer."""

    timeout_seconds: float

    async def review(self, items: Sequence[AiApprovalRequestItem]) -> AiApprovalResponse:
        """Return one allow/deny decision for each supplied item."""
