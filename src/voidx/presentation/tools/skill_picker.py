"""Workspace skill picker helpers for # references."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.skills.service import SkillService


@dataclass(frozen=True)
class SkillToken:
    start: int
    end: int
    query: str


@dataclass(frozen=True)
class SkillCandidate:
    name: str
    scope: str
    description: str
    mode: str


def find_skill_token(text: str, cursor: int) -> SkillToken | None:
    cursor = max(0, min(cursor, len(text)))
    start = text.rfind("#", 0, cursor)
    while start != -1:
        if start == 0 or text[start - 1].isspace():
            break
        start = text.rfind("#", 0, start)
    if start == -1:
        return None
    if start + 1 < len(text) and text[start + 1] == "#":
        return None
    token = text[start + 1:cursor]
    if any(ch.isspace() for ch in token):
        return None
    return SkillToken(start=start, end=cursor, query=token)


def list_skill_candidates(
    query: str,
    limit: int = 8,
    *,
    service: SkillService,
) -> list[SkillCandidate]:
    query_lower = query.strip().lower()
    prefix_matches: list[SkillCandidate] = []
    other_matches: list[SkillCandidate] = []
    for skill in service.enabled_skills():
        if skill.meta.scope not in {"global", "project"}:
            continue
        candidate = SkillCandidate(
            name=skill.name,
            scope=skill.meta.scope,
            description=skill.meta.description.strip(),
            mode=service.mode(skill),
        )
        name_lower = skill.name.lower()
        desc_lower = candidate.description.lower()
        if not query_lower:
            prefix_matches.append(candidate)
        elif name_lower.startswith(query_lower):
            prefix_matches.append(candidate)
        elif query_lower in name_lower or query_lower in desc_lower:
            other_matches.append(candidate)
    prefix_matches.sort(key=_skill_candidate_sort_key)
    other_matches.sort(key=_skill_candidate_sort_key)
    return [*prefix_matches, *other_matches][:limit]


def _skill_candidate_sort_key(candidate: SkillCandidate) -> tuple[int, str]:
    scope_rank = 0 if candidate.scope == "project" else 1
    return (scope_rank, candidate.name.lower())
