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


def test_subagent_transport_lives_in_adapter_package():
    assert not (AGENT / "gateway").exists()
    path = AGENT / "adapters" / "subagent" / "inprocess_gateway.py"
    assert path.exists()
    source = path.read_text(encoding="utf-8")
    assert "class InProcessSubagentGateway" in source


def test_agent_tool_runtime_uses_subagent_transport_port():
    path = AGENT / "adapters" / "tools" / "context.py"
    source = path.read_text(encoding="utf-8")
    assert "subagent_transport: object" not in source
    assert "SubagentTransport" in source


def test_langgraph_subagent_does_not_probe_transport_implementation():
    path = AGENT / "adapters" / "langgraph" / "runtime" / "subagent.py"
    source = path.read_text(encoding="utf-8")
    assert "_gateway_run_by_id" not in source


def test_agent_composition_moved_to_bootstrap():
    assert not (AGENT / "composition.py").exists()
    source = (ROOT / "src" / "voidx" / "bootstrap" / "agent.py").read_text(encoding="utf-8")
    assert "class ApplicationResources" in source
    assert "class AgentResources" in source
    assert "class IntegrationResources" in source
    assert "SimpleNamespace" not in source


def test_automation_services_are_not_dynamically_injected_or_probed():
    paths = [
        AGENT / "adapters" / "input_router.py",
        AGENT / "adapters" / "langgraph" / "execution.py",
        ROOT / "src" / "voidx" / "presentation" / "slash" / "commands" / "loop_cmd.py",
        ROOT / "src" / "voidx" / "presentation" / "slash" / "commands" / "mode.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert 'getattr(self._execution, "loop_service"' not in source
    assert 'getattr(self._execution, "goal_service"' not in source
    assert 'getattr(self, "loop_service"' not in source
    assert 'getattr(self.host, "loop_service"' not in source
    assert 'getattr(self.host, "goal_service"' not in source


def test_parent_result_publisher_is_a_concrete_adapter():
    path = AGENT / "adapters" / "persistence" / "parent_result_publisher.py"
    assert path.exists()
    source = path.read_text(encoding="utf-8")
    assert "class AsyncParentResultPublisher" in source


def test_agent_facade_only_owns_run_loop_entrypoint():
    path = AGENT / "facade.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    facade = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentFacade")
    constructor = next(node for node in facade.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assert [arg.arg for arg in constructor.args.args] == ["self"]
    assert [arg.arg for arg in constructor.args.kwonlyargs] == ["run_loop"]
    assert "_execution" not in source
    assert "Any" not in source
