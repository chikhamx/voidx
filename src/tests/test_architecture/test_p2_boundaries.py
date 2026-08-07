from __future__ import annotations

import ast
from pathlib import Path

from .import_graph import format_edges, import_edges, is_under


ROOT = Path(__file__).resolve().parents[3]


def test_agent_core_does_not_import_persistence() -> None:
    violations = [
        edge
        for edge in import_edges()
        if (
            is_under(edge.source, "voidx.agent.domain")
            or is_under(edge.source, "voidx.agent.application")
            or is_under(edge.source, "voidx.agent.ports")
        )
        and is_under(edge.target, "voidx.persistence")
    ]
    assert violations == [], "agent core imports persistence:\n" + format_edges(violations)


def test_config_profile_port_has_no_global_binding_or_adapter_fallback() -> None:
    path = ROOT / "src/voidx/config/ports.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            forbidden.append(node.lineno)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            forbidden.append(node.lineno)
    assert forbidden == [], f"config profile port contains global binding/factory at lines {forbidden}"
