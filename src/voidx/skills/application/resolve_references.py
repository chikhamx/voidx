"""Resolve explicit skill references through an injected lookup."""

from __future__ import annotations

from voidx.skills.application.ports import SkillLookup
from voidx.skills.domain.references import (
    SkillReferenceMessage,
    SkillReferenceSummary,
    parse_skill_references,
)
from voidx.skills.registry import normalize_skill_name
from voidx.skills.schema import SkillDefinition


class ResolveSkillReferences:
    def __init__(self, lookup: SkillLookup) -> None:
        self._lookup = lookup

    def __call__(self, user_text: str) -> SkillReferenceMessage:
        seen: set[str] = set()
        summaries: list[SkillReferenceSummary] = []
        remove_spans: list[tuple[int, int]] = []

        for reference in parse_skill_references(user_text):
            skill = self._lookup.get(reference.name)
            if skill is None or not self._lookup.is_enabled(skill):
                continue
            name = normalize_skill_name(skill.name)
            remove_spans.append(reference.span)
            if name in seen:
                continue
            seen.add(name)
            summaries.append(self._summary_for(skill))

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

    @staticmethod
    def _summary_for(skill: SkillDefinition) -> SkillReferenceSummary:
        description = skill.meta.description.strip() or "(no description)"
        return SkillReferenceSummary(name=skill.name, description=description)
