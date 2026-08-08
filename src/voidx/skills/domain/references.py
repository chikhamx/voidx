"""Pure parsing and result types for explicit skill references."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SKILL_REFERENCE_RE = re.compile(r"(?<![\w.-])\$([A-Za-z0-9_.-]+)")


@dataclass(frozen=True)
class ParsedSkillReference:
    name: str
    span: tuple[int, int]


@dataclass(frozen=True)
class SkillReferenceSummary:
    name: str
    description: str


@dataclass(frozen=True)
class SkillReferenceMessage:
    prefix: str = ""
    remove_spans: list[tuple[int, int]] = field(default_factory=list)
    skills: list[SkillReferenceSummary] = field(default_factory=list)


def parse_skill_references(text: str) -> list[ParsedSkillReference]:
    if "$" not in text:
        return []
    return [
        ParsedSkillReference(
            name=match.group(1).strip().lower(),
            span=(match.start(), match.end()),
        )
        for match in SKILL_REFERENCE_RE.finditer(text)
    ]
