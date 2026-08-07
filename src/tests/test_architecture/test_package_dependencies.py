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


def test_top_level_dependencies_match_allowlist():
    violations = [
        edge
        for edge in without_debt(import_edges(), "top_level_dependency")
        if top_level(edge.source) != top_level(edge.target)
        and top_level(edge.source) in ALLOWED
        and top_level(edge.target) not in ALLOWED[top_level(edge.source)]
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




def test_agent_tool_adapters_do_not_import_presentation():
    violations = [
        edge
        for edge in import_edges()
        if is_under(edge.source, "voidx.agent.adapters.tools")
        and is_under(edge.target, "voidx.presentation")
    ]
    assert violations == [], "agent tool adapter presentation dependencies:\n" + format_edges(violations)
def test_feature_core_dependencies_are_narrow():
    violations = []
    for edge in without_debt(import_edges(), "feature_core_dependency"):
        source = edge.source
        target = edge.target
        if is_under(source, "voidx.agent.domain") or is_under(source, "voidx.agent.ports"):
            if top_level(target) != "agent":
                violations.append(edge)
        elif is_under(source, "voidx.agent.application"):
            if not (
                top_level(target) == "agent"
                and (is_under(target, "voidx.agent.domain") or is_under(target, "voidx.agent.ports"))
            ) and top_level(target) not in {"platform", "observability"}:
                violations.append(edge)
        elif is_under(source, "voidx.tooling.domain") or is_under(source, "voidx.tooling.ports"):
            if top_level(target) != "tooling":
                violations.append(edge)
        elif is_under(source, "voidx.tooling.application"):
            tooling_core = top_level(target) == "tooling" and (
                is_under(target, "voidx.tooling.domain") or is_under(target, "voidx.tooling.ports")
            )
            filesystem_grant_policy = (
                source in {
                    "voidx.tooling.application.authorization",
                    "voidx.tooling.application.permission_service",
                }
                and target == "voidx.tooling.policy.filesystem.grants"
            )
            if not (tooling_core or filesystem_grant_policy) and top_level(target) not in {"platform", "observability"}:
                violations.append(edge)
    assert violations == [], "feature core dependency violations:\n" + format_edges(violations)
