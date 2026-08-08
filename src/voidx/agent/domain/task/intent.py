"""Shared runtime mode and coarse task intent classification."""

from __future__ import annotations

import re

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
    CODING = "coding"
    GENERAL = "general"


class PersonaName(str, Enum):
    COORDINATE = "coordinate"
    EXPLORE = "explore"
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"


def contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(_contains_hint(text, hint) for hint in hints)


def _contains_hint(text: str, hint: str) -> bool:
    if hint.isascii() and re.fullmatch(r"[A-Za-z0-9_ -]+", hint):
        words = re.findall(r"[A-Za-z0-9_]+", hint)
        if len(words) == 1:
            return re.search(rf"(?<![A-Za-z0-9_]){re.escape(hint)}(?![A-Za-z0-9_])", text) is not None
    return hint in text
