import sys
from pathlib import Path


import pytest

from voidx.llm.compaction import COMPACTION_REQUEST
from voidx.llm.instruction import InstructionService
from voidx.config import Settings
from voidx.skills.registry import SkillRegistry, parse_skill_file
from voidx.workflow.context import WORKFLOW_CONTEXT_MARKER, WORKFLOW_CONTEXT_SCOPE
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_exit_summaries,
    workflow_terminal_condition,
    workflow_transitions,
)
from voidx.workflow.runtime import (
    WorkflowActivationSource,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    advance_workflow_states,
)
from voidx.skills.schema import SkillSelectionConfig
from voidx.skills.references import skill_reference_message
from voidx.skills.service import SkillService
from voidx.ui.tools.skill_picker import list_skill_candidates
from voidx.workflow.service import WorkflowService
from tests.test_skills.conftest import _write_skill



def test_workflow_service_select_from_start_returns_single_match():
    service = WorkflowService()

    review = service.select_from_start("review")
    debug = service.select_from_start("debug")
    unknown = service.select_from_start("nonexistent")

    assert [match.name for match in review] == ["review"]
    assert review[0].reason == "goal_resolver"
    assert [match.name for match in debug] == ["debug"]
    assert debug[0].reason == "goal_resolver"
    assert unknown == []


def test_builtin_workflow_nodes_declare_execution_contracts():
    for node in WorkflowService().nodes():
        assert node.goal
        assert node.persona
        assert node.io.input
        assert node.io.output
        assert not hasattr(node, "tools")
        assert not hasattr(node, "triggers")
        assert not hasattr(node, "priority")
        assert not hasattr(node, "enabled")
        assert not hasattr(node, "core_rule")
        assert not hasattr(node, "decision_rules")
        assert not hasattr(node, "extra_sections")




def test_design_workflow_is_audience_aware():
    service = WorkflowService()
    design = service.get("design")

    assert design is not None
    assert "audience-specific quality gate" in design.goal
    assert design.gate.required_before_transition == "doc passes audience-appropriate quality gate"
    assert "audience" in design.gate.description

    rendered = service.render_instruction(design)
    assert "Identify the audience" in rendered
    assert "Select the document structure" in rendered
    assert "project-provided template when available" in rendered
    assert "templates/readme.md" not in rendered
    assert "Draft for the audience" in rendered
    assert "Execution readiness test" in rendered


def test_workflow_internal_subworkflows_are_structured_and_local():
    service = WorkflowService()

    tdd = service.get("tdd")
    debug = service.get("debug")
    review = service.get("review")
    brainstorm = service.get("brainstorm")

    assert tdd is not None and tdd.subworkflow is not None
    assert tdd.subworkflow.name == "TDD Cycle"
    assert tdd.subworkflow.exit_condition
    assert [step.action for step in tdd.subworkflow.steps][:3] == [
        "Pick the next unimplemented requirement or plan task",
        "Write a failing test",
        "Run the test and confirm RED",
    ]
    assert debug is not None and debug.subworkflow is not None
    assert debug.subworkflow.name == "Debug Cycle"
    assert review is not None and review.subworkflow is not None
    assert review.subworkflow.name == "Review Cycle"
    assert review.subworkflow.description
    assert brainstorm is not None and brainstorm.subworkflow is None


def test_workflow_render_expands_execution_contract():
    service = WorkflowService()
    rendered = service.render_instruction(service.get("tdd"))

    assert "### Goal" in rendered
    assert "### Persona" not in rendered
    assert "### Input" not in rendered
    assert "### Output" not in rendered
    assert "### Tools" not in rendered
    assert "### Available Exits" not in rendered
    assert "### Gate" in rendered
    assert "### Internal Subworkflow: TDD Cycle" in rendered
    assert "Exit condition: all scoped implementation tasks are complete and the relevant test set is green" in rendered
    assert "### Core Rule" not in rendered
    assert "### Decision Rules" not in rendered


def test_workflow_prompts_are_capability_aware_and_repository_agnostic():
    service = WorkflowService()
    brainstorm = service.render_instruction(service.get("brainstorm"))
    plan = service.render_instruction(service.get("plan"))
    review = service.render_instruction(service.get("review"))

    assert "perceived simplicity" not in brainstorm
    assert "too simple to need a design" not in brainstorm.lower()
    assert "docs/specs/" not in plan
    assert "docs/design/" not in plan
    assert "Obey the current interaction mode's write permissions." in plan
    assert "available tool names" in plan
    assert "voidx tool names" not in plan
    assert "Perform the review directly" in review
    assert "when delegation is available" in review
    assert "Delegate to review agent" not in review
    assert "Do not ask for review" not in review

    feedback = service.render_instruction(service.get("feedback"))
    assert "search for actual usage" in feedback
    assert "grep for actual usage" not in feedback


def test_brainstorm_exit_rules_make_small_change_precedence_explicit(tmp_path):
    edges = DEFAULT_WORKFLOW_DAG.edges_from("brainstorm")

    assert edges[0].condition == "approved"
    small_change = next(edge for edge in edges if edge.condition == "small_change")
    assert small_change.target == "tdd"
    assert "local or mechanical" in small_change.description
    skip_descriptions = [
        edge.description
        for edge in edges
        if edge.condition == "skip_to_plan"
    ]
    assert skip_descriptions
    assert all("detailed spec" in item for item in skip_descriptions)


def test_skill_transitions_are_soft_constraints_documented():
    doc = Path(__file__).resolve().parents[3] / "docs" / "archive" / "2026-06" / "2026-06-09" / "skill-state-machine-2026-06-08.md"
    text = doc.read_text(encoding="utf-8")

    assert "transition 是 **soft constraint**" in text
    assert "runtime 不强制推进依赖链" in text
    assert "transition_to: list[str]" in text


def test_workflow_state_summary_includes_transition_hint():
    run = WorkflowRunState(
        name="tdd",
        status=WorkflowRunStatus.ACTIVE,
        source=WorkflowActivationSource.WORKFLOW,
        reason="implement intent",
        transition_to=["verify"],
    )

    assert "next=verify" in run.state_summary()


def test_feedback_workflow_exposes_design_and_plan_exits():
    assert workflow_transitions("feedback") == (
        "tdd",
        "verify",
        "brainstorm",
        "plan",
    )

    edges = {edge.condition: edge for edge in workflow_edges("feedback")}
    assert edges["needs_design"].target == "brainstorm"
    assert edges["needs_plan"].target == "plan"
    assert "design" in edges["needs_design"].description.lower()
    assert "plan" in edges["needs_plan"].description.lower()

    feedback = DEFAULT_WORKFLOW_DAG.nodes["feedback"]
    assert "deferred_items" in feedback.io.output
    step = next(item for item in feedback.workflow if item.order == 6)
    assert "needs_design" in step.description
    assert "needs_plan" in step.description
    assert any("needs_design" in rule for rule in feedback.rules)
    assert any("needs_plan" in rule for rule in feedback.rules)


def test_advance_workflow_states_marks_satisfied_from_evidence():
    run = WorkflowRunState(
        name="tdd",
        status=WorkflowRunStatus.ACTIVE,
        transition_to=[],
    )

    states = advance_workflow_states(
        [run],
        [
            WorkflowStateEvent(
                workflow="tdd",
                kind=WorkflowStateEventKind.SATISFIED,
                ref="tool:pytest",
                ok=True,
                summary="focused tests passed",
                reason="focused tests passed",
                condition="implemented",
            )
        ],
        turn_count=4,
    )

    tdd = next(item for item in states if item.name == "tdd")
    assert tdd.status == WorkflowRunStatus.SATISFIED
    assert tdd.updated_turn == 4
    assert tdd.evidence[0].summary == "focused tests passed"


def test_advance_workflow_states_does_not_mark_pending_satisfied():
    states = advance_workflow_states(
        [WorkflowRunState(name="tdd", status=WorkflowRunStatus.PENDING)],
        [{"workflow": "tdd", "kind": "satisfied"}],
        turn_count=4,
    )

    assert states[0].status == WorkflowRunStatus.PENDING
    assert "verify" not in [run.name for run in states]


def test_workflow_terminal_exit_is_structured_and_terminal():
    condition = workflow_terminal_condition()

    assert condition == DEFAULT_WORKFLOW_DAG.terminal_exit.condition
    assert is_workflow_terminal_condition(f" {condition} ")
    assert DEFAULT_WORKFLOW_DAG.terminal_exit_summary() in workflow_exit_summaries("tdd")

    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                transition_to=["verify"],
            )
        ],
        [
            WorkflowStateEvent(
                workflow="tdd",
                kind=WorkflowStateEventKind.SATISFIED,
                condition=condition,
                reason="terminal state verified",
            )
        ],
    )

    assert [run.name for run in states] == ["tdd"]
    assert states[0].status == WorkflowRunStatus.SATISFIED


def test_advance_workflow_states_initializes_missing_run_from_event_kind():
    blocked = advance_workflow_states(
        [],
        [{"workflow": "debug", "kind": "blocked", "reason": "needs repro"}],
    )
    skipped = advance_workflow_states(
        [],
        [{"workflow": "review", "kind": "skipped"}],
    )
    satisfied = advance_workflow_states(
        [],
        [{"workflow": "tdd", "kind": "satisfied"}],
    )

    assert blocked[0].status == WorkflowRunStatus.BLOCKED
    assert blocked[0].blocked_reason == "needs repro"
    assert skipped[0].status == WorkflowRunStatus.SKIPPED
    assert satisfied[0].status == WorkflowRunStatus.PENDING


def test_advance_workflow_states_activates_transition_target():
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="feature",
                scope="runtime",
                transition_to=["verify"],
            )
        ],
        [
            {
                "workflow": "tdd",
                "kind": "satisfied",
                "summary": "implementation complete",
                "reason": "focused tests passed",
                "condition": "implemented",
            }
        ],
        turn_count=5,
    )

    by_name = {run.name: run for run in states}
    assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
    successor = by_name["verify"]
    assert successor.status == WorkflowRunStatus.ACTIVE
    assert successor.source == WorkflowActivationSource.TRANSITION
    assert successor.reason == "transition from tdd via implemented"
    assert successor.goal_type == "feature"
    assert successor.scope == "runtime"
    assert successor.personas == ["review"]


@pytest.mark.parametrize(
    ("condition", "target", "persona"),
    [
        ("needs_design", "brainstorm", ["explore"]),
        ("needs_plan", "plan", ["plan"]),
    ],
)
def test_advance_workflow_states_routes_feedback_to_deferred_workflow(condition, target, persona):
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="feedback",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="review",
                scope="review feedback",
            )
        ],
        [
            WorkflowStateEvent(
                workflow="feedback",
                kind=WorkflowStateEventKind.SATISFIED,
                summary="actionable feedback implemented; remaining item deferred",
                reason="remaining feedback requires design or planning",
                condition=condition,
            )
        ],
        turn_count=8,
    )

    by_name = {run.name: run for run in states}
    assert by_name["feedback"].status == WorkflowRunStatus.SATISFIED
    successor = by_name[target]
    assert successor.status == WorkflowRunStatus.ACTIVE
    assert successor.source == WorkflowActivationSource.TRANSITION
    assert successor.reason == f"transition from feedback via {condition}"
    assert successor.goal_type == "review"
    assert successor.scope == "review feedback"
    assert successor.personas == persona


def test_advance_workflow_states_does_not_advance_without_evidence():
    run = WorkflowRunState(
        name="tdd",
        status=WorkflowRunStatus.ACTIVE,
        transition_to=["verify"],
    )

    states = advance_workflow_states([run], [], turn_count=6)

    assert [item.name for item in states] == ["tdd"]
    assert states[0].status == WorkflowRunStatus.ACTIVE


def test_advance_workflow_states_does_not_repeat_transition_from_satisfied_node():
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.SATISFIED,
                transition_to=["verify"],
            )
        ],
        [
            WorkflowStateEvent(
                workflow="tdd",
                kind=WorkflowStateEventKind.SATISFIED,
                reason="duplicate completion signal",
                condition="implemented",
            )
        ],
    )

    assert [run.name for run in states] == ["tdd"]
    assert states[0].status == WorkflowRunStatus.SATISFIED


def test_advance_workflow_states_rejects_invalid_condition_without_satisfying_node():
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                transition_to=["verify"],
            )
        ],
        [
            WorkflowStateEvent(
                workflow="tdd",
                kind=WorkflowStateEventKind.SATISFIED,
                reason="invalid condition should not advance",
                condition="approved",
            )
        ],
    )

    assert [run.name for run in states] == ["tdd"]
    assert states[0].status == WorkflowRunStatus.ACTIVE


def test_advance_workflow_states_allows_empty_evidence_before_transition():
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                transition_to=["verify"],
            )
        ],
        [
            WorkflowStateEvent(
                workflow="tdd",
                kind=WorkflowStateEventKind.SATISFIED,
                condition="implemented",
            )
        ],
    )

    by_name = {run.name: run for run in states}
    assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
    assert by_name["verify"].status == WorkflowRunStatus.ACTIVE


def test_advance_workflow_states_does_not_duplicate_existing_successor():
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                transition_to=["verify"],
            ),
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
                reason="implement lifecycle",
            ),
        ],
        [
            {
                "workflow": "tdd",
                "kind": "satisfied",
                "reason": "focused tests passed",
                "condition": "implemented",
            }
        ],
    )

    assert [run.name for run in states].count("verify") == 1
    verification = next(run for run in states if run.name == "verify")
    assert verification.reason == "implement lifecycle"


def test_blocked_or_skipped_workflow_does_not_trigger_successor():
    blocked = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.BLOCKED,
                transition_to=["verify"],
            )
        ],
        [{"workflow": "tdd", "kind": "satisfied"}],
    )
    skipped = advance_workflow_states(
        [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                transition_to=["verify"],
            )
        ],
        [{"workflow": "tdd", "kind": "skipped"}],
    )

    assert [run.name for run in blocked] == ["tdd"]
    assert blocked[0].status == WorkflowRunStatus.BLOCKED
    assert [run.name for run in skipped] == ["tdd"]
    assert skipped[0].status == WorkflowRunStatus.SKIPPED


def test_blocked_workflow_can_reactivate_when_condition_clears():
    states = advance_workflow_states(
        [
            WorkflowRunState(
                name="debug",
                status=WorkflowRunStatus.BLOCKED,
                blocked_reason="needs repro",
            )
        ],
        [
            WorkflowStateEvent(
                workflow="debug",
                kind=WorkflowStateEventKind.UNBLOCKED,
                summary="repro added",
            )
        ],
        turn_count=7,
    )

    assert states[0].status == WorkflowRunStatus.ACTIVE
    assert states[0].blocked_reason == ""
    assert states[0].updated_turn == 7
