"""Helpers for explicit skill references in user messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from voidx.config import Settings
from voidx.skills.registry import SkillRegistry, normalize_skill_name
from voidx.skills.schema import EXPLICIT_REF_RE, SkillDefinition
from voidx.skills.service import SkillService


@dataclass(frozen=True)
class SkillReferenceSummary:
    name: str
    description: str


@dataclass(frozen=True)
class SkillReferenceMessage:
    prefix: str = ""
    remove_spans: list[tuple[int, int]] = field(default_factory=list)
    skills: list[SkillReferenceSummary] = field(default_factory=list)


def skill_reference_message(
    user_text: str,
    workspace: str,
    *,
    settings: Settings | None = None,
    service: SkillService | None = None,
) -> SkillReferenceMessage:
    if "$" not in user_text:
        return SkillReferenceMessage()

    if service is None:
        settings = settings or Settings(workspace)
        service = SkillService(
            SkillRegistry(str(Path(workspace).resolve())),
            selection=settings.get_skill_selection(),
        )
    seen: set[str] = set()
    summaries: list[SkillReferenceSummary] = []
    remove_spans: list[tuple[int, int]] = []

    for match in EXPLICIT_REF_RE.finditer(user_text):
        skill = service.get(match.group(1))
        if skill is None or not service.is_enabled(skill):
            continue
        name = normalize_skill_name(skill.name)
        remove_spans.append((match.start(), match.end()))
        if name in seen:
            continue
        seen.add(name)
        summaries.append(_summary_for(skill))

    if not summaries:
        return SkillReferenceMessage(remove_spans=remove_spans)
    prefix = "Explicit skills requested:\n" + "\n".join(
        f"- {summary.name}: {summary.description}"
        for summary in summaries
    )
    prefix += (
        "\n\nBefore acting, call skill with op='load' for each listed skill. "
        "Descriptions are index metadata, not the full instructions."
    )
    return SkillReferenceMessage(
        prefix=prefix,
        remove_spans=remove_spans,
        skills=summaries,
    )


def _summary_for(skill: SkillDefinition) -> SkillReferenceSummary:
    description = skill.meta.description.strip() or "(no description)"
    return SkillReferenceSummary(name=skill.name, description=description)
