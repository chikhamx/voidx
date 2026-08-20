"""Builtin workflow metadata projection for configuration UIs."""

from __future__ import annotations

from typing import Any

from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG


def builtin_workflow_catalog() -> dict[str, Any]:
    return {
        "builtin_nodes": [
            {"name": node.name, "description": node.description}
            for node in DEFAULT_WORKFLOW_DAG.nodes.values()
        ],
        "default_edges": [
            {
                "source": edge.source,
                "target": edge.target,
                "condition": edge.condition,
                "label": edge.label,
            }
            for edge in DEFAULT_WORKFLOW_DAG.edges
        ],
    }


__all__ = ["builtin_workflow_catalog"]
