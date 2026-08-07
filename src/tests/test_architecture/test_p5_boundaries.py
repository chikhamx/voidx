"""P5 subagent gateway and explicit composition architecture guards."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
AGENT = ROOT / "src" / "voidx" / "agent"


def test_subagent_domain_and_transport_port_exist():
    assert (AGENT / "domain" / "subagent.py").exists()
    assert (AGENT / "ports" / "subagent.py").exists()


def test_legacy_gateway_models_do_not_own_subagent_dtos():
    path = AGENT / "gateway" / "models.py"
    if not path.exists():
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert {"AgentRun", "AgentMessage"}.isdisjoint(classes)


def test_asyncio_transport_does_not_own_subagent_route_policy():
    path = AGENT / "gateway" / "gateway.py"
    if not path.exists():
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_validate_route" not in methods
    assert "_validate_send_open" not in methods


def test_subagent_transport_port_is_explicit():
    path = AGENT / "ports" / "subagent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    protocols = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
    }
    assert "SubagentTransport" in protocols
    assert "ParentResultPublisher" in protocols
