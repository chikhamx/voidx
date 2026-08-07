from __future__ import annotations

from .import_graph import format_edges, import_edges, is_under


def test_agent_domain_does_not_import_application() -> None:
    violations = [
        edge
        for edge in import_edges()
        if is_under(edge.source, "voidx.agent.domain")
        and is_under(edge.target, "voidx.agent.application")
    ]
    assert violations == [], "agent domain imports application:\n" + format_edges(violations)


def test_loop_prompt_materializer_uses_domain_attachment_contract() -> None:
    violations = [
        edge
        for edge in import_edges()
        if is_under(
            edge.source,
            "voidx.agent.application.automation.loop.prompt_materialize",
        )
        and is_under(edge.target, "voidx.agent.application.attachments")
    ]
    assert violations == [], "loop imports application attachments:\n" + format_edges(violations)
