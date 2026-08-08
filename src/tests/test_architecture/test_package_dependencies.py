from __future__ import annotations

from .import_graph import format_edges, import_edges, is_under, top_level, without_debt


ALLOWED: dict[str, set[str]] = {
    "platform": set(),
    "observability": {"platform"},
    "persistence": {"platform", "observability"},
    "llm": {"platform", "observability"},
    "skills": {"platform", "observability"},
    "lsp": {"platform", "observability"},
    "mcp": {"platform", "observability"},
    "tooling": {"llm", "mcp", "lsp", "skills", "platform", "observability"},
    "agent": {
        "llm", "mcp", "lsp", "skills", "tooling", "persistence", "platform", "observability"
    },
    "config": {
        "agent", "llm", "mcp", "lsp", "skills", "tooling", "persistence", "platform", "observability"
    },
    "update": {"config", "platform", "observability"},
    "presentation": {
        "agent", "config", "mcp", "lsp", "skills", "tooling", "persistence", "platform", "observability", "update"
    },
    "bootstrap": {
        "agent", "config", "llm", "mcp", "lsp", "skills", "tooling", "persistence", "platform", "observability", "presentation", "update"
    },
    "main": {"bootstrap"},
    "data": set(),
}


def _top_level_dependency_allowed(source: str, target: str) -> bool:
    source_top = top_level(source)
    target_top = top_level(target)
    return source_top in ALLOWED and (
        source_top == target_top or target_top in ALLOWED[source_top]
    )


def test_unknown_top_level_packages_are_forbidden() -> None:
    assert not _top_level_dependency_allowed(
        "voidx.unowned.feature",
        "voidx.platform.paths",
    )


def test_top_level_dependencies_match_allowlist():
    violations = [
        edge
        for edge in without_debt(import_edges(), "top_level_dependency")
        if not _top_level_dependency_allowed(edge.source, edge.target)
    ]
    assert violations == [], "forbidden top-level dependencies:\n" + format_edges(violations)


def test_core_layers_do_not_import_presentation():
    violations = [
        edge
        for edge in without_debt(import_edges(), "presentation_leak")
        if top_level(edge.target) == "presentation"
        and top_level(edge.source) in {"agent", "tooling", "mcp", "llm", "lsp", "skills"}
    ]
    assert violations == [], "core presentation leaks:\n" + format_edges(violations)




def test_presentation_depends_only_on_agent_domain_ports_or_facade() -> None:
    violations = [
        edge
        for edge in import_edges()
        if is_under(edge.source, "voidx.presentation")
        and (
            is_under(edge.target, "voidx.agent.application")
            or is_under(edge.target, "voidx.agent.adapters")
        )
    ]
    assert violations == [], "presentation agent implementation dependencies:\n" + format_edges(violations)


def test_agent_tool_adapters_do_not_import_presentation():
    violations = [
        edge
        for edge in import_edges()
        if is_under(edge.source, "voidx.agent.adapters.tools")
        and is_under(edge.target, "voidx.presentation")
    ]
    assert violations == [], "agent tool adapter presentation dependencies:\n" + format_edges(violations)
def _feature_core_dependency_allowed(source: str, target: str) -> bool:
    target_top = top_level(target)
    if is_under(source, "voidx.agent.domain") or is_under(source, "voidx.agent.ports"):
        return target_top == "agent"
    if is_under(source, "voidx.agent.application"):
        agent_core = target_top == "agent" and not is_under(target, "voidx.agent.adapters")
        return agent_core or target_top in {"platform", "observability"}
    if is_under(source, "voidx.tooling.domain") or is_under(source, "voidx.tooling.ports"):
        return target_top == "tooling"
    if is_under(source, "voidx.tooling.application"):
        tooling_core = target_top == "tooling" and not is_under(target, "voidx.tooling.adapters")
        return tooling_core or target_top in {"platform", "observability"}
    return True


def test_same_feature_application_dependencies_are_allowed() -> None:
    assert _feature_core_dependency_allowed(
        "voidx.agent.application.runtime.dispatcher",
        "voidx.agent.application.runtime.recovery",
    )
    assert _feature_core_dependency_allowed(
        "voidx.tooling.application.authorization",
        "voidx.tooling.application.permission_service",
    )


def test_feature_application_adapter_dependencies_are_forbidden() -> None:
    assert not _feature_core_dependency_allowed(
        "voidx.agent.application.chat_service",
        "voidx.agent.adapters.persistence.session_repository",
    )


def test_feature_core_dependencies_are_narrow():
    violations = [
        edge
        for edge in without_debt(import_edges(), "feature_core_dependency")
        if not _feature_core_dependency_allowed(edge.source, edge.target)
    ]
    assert violations == [], "feature core dependency violations:\n" + format_edges(violations)
