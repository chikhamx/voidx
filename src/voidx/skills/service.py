"""Runtime skill selection and instruction rendering."""

from __future__ import annotations

import re
from collections.abc import Iterable

from voidx.skills.registry import SkillRegistry, normalize_skill_name
from voidx.skills.schema import SkillDefinition, SkillMatch, SkillScope, SkillSelectionConfig
from voidx.skills.context import render_skill_instruction

_EXPLICIT_REF_RE = re.compile(r"(?<![\w.-])\$([A-Za-z0-9_.-]+)")


class SkillService:
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        selection: SkillSelectionConfig | None = None,
    ) -> None:
        self._registry = registry
        self._selection = selection or SkillSelectionConfig()

    def list_skills(self) -> list[SkillDefinition]:
        return self._registry.discover()

    def get(self, name: str) -> SkillDefinition | None:
        return self._registry.get(name)

    def enabled_skills(self) -> list[SkillDefinition]:
        return [skill for skill in self.list_skills() if self.is_enabled(skill)]

    def enabled_bundled_skills(self) -> list[SkillDefinition]:
        return [
            skill for skill in self.enabled_skills()
            if skill.meta.scope == "bundled"
        ]

    def is_enabled(self, skill: SkillDefinition) -> bool:
        name = normalize_skill_name(skill.name)
        if name in self._normalized(self._selection.disabled):
            return False
        if name in self._normalized(self._selection.enabled):
            return True
        return skill.meta.enabled

    def select(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        interaction_mode: str | None = None,
        limit: int = 5,
        scopes: Iterable[SkillScope] | None = None,
        exclude_names: Iterable[str] = (),
    ) -> list[SkillMatch]:
        text = user_text.strip()
        has_context = bool(agent or task_intent or interaction_mode)
        if not text and not has_context:
            return []

        allowed_scopes = set(scopes) if scopes is not None else None
        excluded = self._normalized(exclude_names)
        skills = [
            skill for skill in self.enabled_skills()
            if (allowed_scopes is None or skill.meta.scope in allowed_scopes)
            and normalize_skill_name(skill.name) not in excluded
        ]
        skills_by_name = {normalize_skill_name(skill.name): skill for skill in skills}
        explicit = self._explicit_refs(text)
        matches: list[SkillMatch] = []
        seen: set[str] = set()

        def add_match(skill: SkillDefinition | None, reason: str) -> None:
            if skill is None:
                return
            name = normalize_skill_name(skill.name)
            if name in seen:
                return
            seen.add(name)
            matches.append(SkillMatch(skill=skill, reason=reason))

        if explicit:
            for name in sorted(explicit):
                add_match(skills_by_name.get(name), "explicit")
            return matches[:limit]

        text_matches: list[SkillMatch] = []
        lowered = text.lower()
        for skill in skills:
            if normalize_skill_name(skill.name) in seen:
                continue
            reason = self._match_reason(skill, lowered)
            if reason:
                text_matches.append(SkillMatch(skill=skill, reason=reason))
        text_matches.sort(key=lambda match: match.name)
        matches.extend(text_matches)
        return matches[:limit]

    def activation_summaries(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        interaction_mode: str | None = None,
        limit: int = 5,
        scopes: Iterable[SkillScope] | None = None,
        exclude_names: Iterable[str] = (),
    ) -> list[str]:
        return [
            f"{match.name} ({match.reason})"
            for match in self.select(
                user_text,
                agent=agent,
                task_intent=task_intent,
                interaction_mode=interaction_mode,
                limit=limit,
                scopes=scopes,
                exclude_names=exclude_names,
            )
        ]

    def instructions_for(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        interaction_mode: str | None = None,
        limit: int = 5,
        scopes: Iterable[SkillScope] | None = None,
        exclude_names: Iterable[str] = (),
    ) -> list[str]:
        return [
            self.render_instruction(match.skill)
            for match in self.select(
                user_text,
                agent=agent,
                task_intent=task_intent,
                interaction_mode=interaction_mode,
                limit=limit,
                scopes=scopes,
                exclude_names=exclude_names,
            )
        ]

    def available_skill_summaries(self, *, include_bundled: bool = False) -> list[str]:
        summaries: list[str] = []
        for skill in self.enabled_skills():
            if skill.meta.scope == "bundled" and not include_bundled:
                continue
            description = skill.meta.description.strip() or "(no description)"
            summaries.append(f"- {skill.name}: {description}")
        return summaries

    @staticmethod
    def render_instruction(skill: SkillDefinition) -> str:
        return render_skill_instruction(skill)

    @staticmethod
    def _normalized(values: Iterable[str]) -> set[str]:
        return {normalize_skill_name(value) for value in values}

    @staticmethod
    def _explicit_refs(text: str) -> set[str]:
        return {normalize_skill_name(match.group(1)) for match in _EXPLICIT_REF_RE.finditer(text)}

    @staticmethod
    def _match_reason(skill: SkillDefinition, lowered_text: str) -> str:
        name = normalize_skill_name(skill.name)
        if _contains_phrase(lowered_text, name):
            return "name"

        for trigger in skill.meta.triggers:
            normalized = trigger.strip().lower()
            if normalized and _contains_phrase(lowered_text, normalized):
                return f"trigger:{trigger}"

        description_terms = _significant_terms(skill.meta.description)
        if description_terms and sum(1 for term in description_terms if _contains_phrase(lowered_text, term)) >= 2:
            return "description"
        return ""


def _contains_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    if _is_cjk_phrase(phrase):
        return phrase in text
    pattern = r"(?<![\w.-])" + re.escape(phrase) + r"(?![\w.-])"
    return bool(re.search(pattern, text))


def _is_cjk_phrase(phrase: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in phrase)


def _significant_terms(text: str) -> set[str]:
    stop = {
        "with",
        "from",
        "that",
        "this",
        "when",
        "into",
        "using",
        "skill",
        "skills",
    }
    return {
        term
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text.lower())
        if term not in stop
    }
