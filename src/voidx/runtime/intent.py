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


_CODING_HINTS = (
    "agent",
    "api",
    "architecture",
    "bug",
    "build",
    "code",
    "commit",
    "debug",
    "design",
    "diff",
    "doc",
    "docs",
    "error",
    "exception",
    "fix",
    "implementation",
    "implement",
    "issue",
    "lint",
    "log",
    "patch",
    "pr",
    "pull request",
    "refactor",
    "release",
    "repo",
    "review",
    "runtime",
    "schema",
    "spec",
    "stacktrace",
    "test",
    "traceback",
    "workflow",
    "\u4ee3\u7801",
    "\u4ed3\u5e93",
    "\u4fee\u590d",
    "\u5b9e\u73b0",
    "\u4fee\u6539",
    "\u91cd\u6784",
    "\u8bbe\u8ba1",
    "\u65b9\u6848",
    "\u67b6\u6784",
    "\u600e\u4e48\u6539",
    "\u5982\u4f55\u6539",
    "\u8c03\u8bd5",
    "\u6392\u67e5",
    "\u62a5\u9519",
    "\u5931\u8d25",
    "\u9519\u8bef",
    "\u6d4b\u8bd5",
    "\u5355\u6d4b",
    "\u6587\u6863",
    "\u89c4\u683c",
    "\u89c4\u5212",
    "\u5ba1\u67e5",
    "\u8bc4\u5ba1",
    "\u770b\u770b",
    "\u770b\u4e00\u4e0b",
    "\u68c0\u67e5",
    "\u68b3\u7406",
    "\u4f18\u5316",
    "\u843d\u5730",
    "\u76f4\u63a5\u6539",
    "\u7ee7\u7eed\u6539",
    "\u7ee7\u7eed\u505a",
    "\u7ee7\u7eed\u5b9e\u73b0",
)

_GENERAL_HINTS = (
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank you",
    "appreciate it",
    "sounds good",
    "good to know",
    "makes sense",
    "nice",
    "\u4f60\u597d",
    "\u8c22\u8c22",
    "\u8f9b\u82e6\u4e86",
    "\u597d\u7684",
    "\u597d\u4e86",
    "\u5bf9\u53ef\u4ee5",
    "\u5bf9\uff0c\u53ef\u4ee5",
    "\u55ef",
    "\u6536\u5230",
)

_CODEISH_PATTERNS = (
    r"\b[a-zA-Z_][\w.-]*/[a-zA-Z_][\w./-]*\b",
    r"\b[\w./-]+\.(py|ts|tsx|js|jsx|rs|go|java|kt|swift|md|toml|yaml|yml|json|sql|sh)\b",
    r"\b[a-zA-Z_][\w.]*\(\)",
    r"`[^`]+`",
)


def infer_task_intent(text: str, interaction_mode: str | InteractionMode | None = None) -> TaskIntent:
    mode = InteractionMode.parse(interaction_mode)
    if mode == InteractionMode.PLAN:
        return TaskIntent.CODING

    normalized = text.lower()
    if not normalized.strip():
        return TaskIntent.GENERAL
    if _contains_any(normalized, _CODING_HINTS):
        return TaskIntent.CODING
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _CODEISH_PATTERNS):
        return TaskIntent.CODING
    if _contains_any(normalized, _GENERAL_HINTS):
        return TaskIntent.GENERAL
    return TaskIntent.CODING


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(_contains_hint(text, hint) for hint in hints)


def _contains_hint(text: str, hint: str) -> bool:
    if hint.isascii() and re.fullmatch(r"[A-Za-z0-9_ -]+", hint):
        words = re.findall(r"[A-Za-z0-9_]+", hint)
        if len(words) == 1:
            return re.search(rf"(?<![A-Za-z0-9_]){re.escape(hint)}(?![A-Za-z0-9_])", text) is not None
    return hint in text
