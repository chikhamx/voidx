"""Shared runtime mode and task intent classification."""

from __future__ import annotations

from enum import Enum


class InteractionMode(str, Enum):
    AUTO = "auto"
    PLAN = "plan"
    GOAL = "goal"

    @classmethod
    def parse(cls, value: str | "InteractionMode" | None) -> "InteractionMode":
        if isinstance(value, cls):
            return value
        if not value:
            return cls.AUTO
        normalized = str(value).strip().lower()
        for mode in cls:
            if mode.value == normalized:
                return mode
        raise ValueError(f"Invalid interaction mode: {value}")

    @property
    def denies_writes(self) -> bool:
        return self == InteractionMode.PLAN


class TaskIntent(str, Enum):
    CHAT = "chat"
    INSPECT = "inspect"
    DESIGN = "design"
    REVIEW = "review"
    IMPLEMENT = "implement"
    DEBUG = "debug"
    AMBIGUOUS = "ambiguous"


_IMPLEMENT_HINTS = (
    "fix", "implement", "change", "edit", "write", "refactor", "patch",
    "apply", "do it", "go ahead", "start coding",
    "\u4fee\u590d", "\u5b9e\u73b0", "\u4fee\u6539", "\u6539\u4e00\u4e0b",
    "\u76f4\u63a5\u6539", "\u5f00\u59cb\u5e72", "\u5f00\u59cb\u505a",
    "\u52a8\u624b", "\u843d\u5730", "\u7ee7\u7eed\u6539",
    "\u7ee7\u7eed\u505a", "\u7ee7\u7eed\u5b9e\u73b0",
    "\u7ee7\u7eed\u4fee\u590d", "\u53ef\u4ee5\u6539",
    "\u53ef\u4ee5\u5f00\u59cb",
)
_DESIGN_HINTS = (
    "design", "plan", "proposal", "approach", "architecture", "suggest",
    "\u8bbe\u8ba1", "\u65b9\u6848", "\u5efa\u8bae", "\u600e\u4e48\u6539",
    "\u5982\u4f55\u6539", "\u8ba8\u8bba", "\u89c4\u5212",
)
_INSPECT_HINTS = (
    "look at", "inspect", "analyze", "explain", "understand", "check",
    "what is", "why", "how does",
    "\u770b\u770b", "\u770b\u4e00\u4e0b", "\u5206\u6790", "\u68b3\u7406",
    "\u4e86\u89e3", "\u68c0\u67e5", "\u73b0\u72b6", "\u662f\u4ec0\u4e48",
    "\u4e3a\u4ec0\u4e48",
)
_REVIEW_HINTS = ("review", "\u5ba1\u67e5", "\u590d\u6838", "\u8bc4\u5ba1")
_DEBUG_HINTS = ("debug", "bug", "error", "traceback", "\u62a5\u9519", "\u6392\u67e5", "\u95ee\u9898")


def infer_task_intent(text: str, interaction_mode: str | InteractionMode | None = None) -> TaskIntent:
    mode = InteractionMode.parse(interaction_mode)
    if mode == InteractionMode.PLAN:
        return TaskIntent.DESIGN

    normalized = text.lower()
    if _contains_any(normalized, _IMPLEMENT_HINTS):
        return TaskIntent.IMPLEMENT
    if _contains_any(normalized, _REVIEW_HINTS):
        return TaskIntent.REVIEW
    if _contains_any(normalized, _DEBUG_HINTS):
        return TaskIntent.DEBUG
    if _contains_any(normalized, _DESIGN_HINTS):
        return TaskIntent.DESIGN
    if _contains_any(normalized, _INSPECT_HINTS):
        return TaskIntent.INSPECT
    return TaskIntent.CHAT


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)
