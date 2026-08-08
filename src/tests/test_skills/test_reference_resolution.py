from __future__ import annotations

from pathlib import Path

from voidx.skills.application.resolve_references import ResolveSkillReferences
from voidx.skills.domain.references import parse_skill_references
from voidx.skills.schema import SkillDefinition, SkillMeta


def skill(name: str, description: str = "") -> SkillDefinition:
    return SkillDefinition(
        meta=SkillMeta(name=name, description=description),
        path=Path(f"/{name}/SKILL.md"),
        body="body",
    )


class Lookup:
    def __init__(self, skills, disabled=()):
        self.skills = {item.name: item for item in skills}
        self.disabled = set(disabled)

    def get(self, name: str):
        return self.skills.get(name)

    def is_enabled(self, item: SkillDefinition) -> bool:
        return item.name not in self.disabled


def test_parser_returns_normalized_names_and_exact_spans() -> None:
    references = parse_skill_references("use $Docs, then $docs and $not-a-skill")

    assert [(ref.name, ref.span) for ref in references] == [
        ("docs", (4, 9)),
        ("docs", (16, 21)),
        ("not-a-skill", (26, 38)),
    ]


def test_resolver_deduplicates_summaries_but_removes_every_valid_span() -> None:
    result = ResolveSkillReferences(Lookup([skill("docs", "Write docs")]))(
        "use $Docs then $docs"
    )

    assert result.remove_spans == [(4, 9), (15, 20)]
    assert [(item.name, item.description) for item in result.skills] == [
        ("docs", "Write docs")
    ]
    assert result.prefix.startswith("Explicit skills requested:\n- docs: Write docs")


def test_resolver_keeps_unknown_and_disabled_references_in_text() -> None:
    result = ResolveSkillReferences(
        Lookup([skill("disabled")], disabled={"disabled"})
    )("keep $unknown and $disabled")

    assert result.prefix == ""
    assert result.remove_spans == []
    assert result.skills == []


def test_execution_resolves_references_with_current_turn_workspace(monkeypatch) -> None:
    from types import SimpleNamespace

    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    requested: list[str] = []
    borrowed_result = object()
    borrowed_api = SimpleNamespace(
        resolve_references=lambda _text: borrowed_result,
    )
    execution = LangGraphExecution.__new__(LangGraphExecution)
    execution._workspace = "/base"
    execution.skills_api = SimpleNamespace(
        resolve_references=lambda _text: (_ for _ in ()).throw(
            AssertionError("base workspace API must not be used")
        )
    )
    execution.skills_api_provider = lambda workspace: (
        requested.append(workspace) or borrowed_api
    )
    monkeypatch.setattr(
        "voidx.agent.adapters.langgraph.execution.current_thread_execution_state",
        lambda: SimpleNamespace(workspace="/borrowed"),
    )

    assert execution._resolve_skill_references("use $docs") is borrowed_result
    assert requested == ["/borrowed"]
