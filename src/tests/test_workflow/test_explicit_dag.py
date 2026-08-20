"""Workflow policy/service must take an explicit DAG; no hidden default."""

from __future__ import annotations

import ast
from inspect import signature
from pathlib import Path

import pytest

from voidx.agent.application.automation.workflow.service import WorkflowService
from voidx.agent.domain.automation.workflow_policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_exit_summaries,
    workflow_gate,
    workflow_personas,
    workflow_sort_key,
    workflow_terminal_condition,
    workflow_terminal_description,
    workflow_transitions,
)
from voidx.agent.domain.automation.workflow_schema import (
    Edge,
    NodeGate,
    NodeIO,
    WorkflowDAG,
    WorkflowNode,
)


ROOT = Path(__file__).resolve().parents[3]


def _tiny_dag() -> WorkflowDAG:
    return WorkflowDAG(
        name="tiny",
        nodes={
            "alpha": WorkflowNode(
                name="alpha",
                goal="do alpha",
                description="alpha node",
                io=NodeIO(input={"q": "q"}, output={"a": "a"}),
                persona="explore",
                gate=NodeGate(description="gate", required_before_transition="done"),
            ),
            "beta": WorkflowNode(
                name="beta",
                goal="do beta",
                description="beta node",
                io=NodeIO(input={"q": "q"}, output={"a": "a"}),
                persona="implement",
            ),
        },
        edges=[
            Edge(source="alpha", target="beta", condition="ready", label="go"),
        ],
    )


def test_workflow_service_requires_explicit_dag():
    with pytest.raises(TypeError):
        WorkflowService()


def test_workflow_service_uses_supplied_dag_only():
    service = WorkflowService(_tiny_dag())

    assert [node.name for node in service.nodes()] == ["alpha", "beta"]
    assert service.get("alpha") is not None
    assert service.get("tdd") is None
    assert [match.name for match in service.select_from_start("alpha")] == ["alpha"]
    assert service.select_from_start("tdd") == []


def test_workflow_policy_queries_use_supplied_dag():
    dag = _tiny_dag()

    assert workflow_transitions("alpha", dag) == ("beta",)
    assert [edge.target for edge in workflow_edges("alpha", dag)] == ["beta"]
    assert workflow_personas("alpha", dag) == ("explore",)
    assert workflow_gate("alpha", dag) is not None
    assert workflow_terminal_condition(dag) == "done"
    assert "ready -> beta (go)" in workflow_exit_summaries("alpha", dag)
    assert is_workflow_terminal_condition("done", dag)
    assert not is_workflow_terminal_condition("ready", dag)
    assert workflow_sort_key("alpha", dag)[1] == "alpha"
    assert workflow_terminal_description(dag)


def test_workflow_policy_without_dag_does_not_fall_back_to_default():
    with pytest.raises(TypeError):
        workflow_transitions("tdd")
    with pytest.raises(TypeError):
        workflow_edges("tdd")
    with pytest.raises(TypeError):
        is_workflow_terminal_condition("done")
    with pytest.raises(TypeError):
        workflow_terminal_condition()


def test_workflow_policy_has_no_module_level_default_dag_cache():
    source = (ROOT / "src/voidx/agent/domain/automation/workflow_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assigned.update(
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )
    assert "WORKFLOW_TRANSITIONS" not in assigned
    assert "DEFAULT_WORKFLOW_DAG" not in source


def test_workflow_service_constructor_has_no_default_dag_parameter():
    params = signature(WorkflowService.__init__).parameters
    assert "dag" in params
    assert params["dag"].default is params["dag"].empty
    source = (ROOT / "src/voidx/agent/application/automation/workflow/service.py").read_text(encoding="utf-8")
    assert "DEFAULT_WORKFLOW_DAG" not in source
    assert "from voidx.agent.domain.automation.workflow_dag import" not in source
