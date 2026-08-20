from __future__ import annotations

from voidx.agent.domain.automation.workflow_catalog import builtin_workflow_catalog


def test_builtin_workflow_catalog_lists_eight_nodes_with_edges() -> None:
    catalog = builtin_workflow_catalog()
    names = {node["name"] for node in catalog["builtin_nodes"]}
    assert names == {
        "brainstorm",
        "design",
        "plan",
        "tdd",
        "verify",
        "review",
        "feedback",
        "debug",
    }
    for node in catalog["builtin_nodes"]:
        assert node["description"].strip()
    edges = catalog["default_edges"]
    assert edges, "default DAG edges must be exposed for subset inheritance"
    for edge in edges:
        assert edge["source"] in names
        assert edge["target"] in names
        assert edge["condition"].strip()
    assert any(
        edge["source"] == "brainstorm" and edge["target"] == "design"
        for edge in edges
    )
