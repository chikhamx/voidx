import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from voidx.llm.compaction import COMPACTION_REQUEST
from voidx.llm.instruction import InstructionService
from voidx.config import Settings
from voidx.skills.registry import SkillRegistry, parse_skill_file
from voidx.workflow.context import WORKFLOW_CONTEXT_MARKER, WORKFLOW_CONTEXT_SCOPE
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import (
    is_workflow_terminal_condition,
    workflow_denied_tools,
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


def _write_skill(root: Path, dirname: str, text: str) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_skill_file_reads_frontmatter_and_body(tmp_path):
    path = _write_skill(
        tmp_path,
        "docs",
        """---
name: docs-helper
description: Helps write docs
enabled: false
triggers:
  - documentation
  - README
---
# Instructions
Write clear docs.
""",
    )

    skill = parse_skill_file(path, scope="project")

    assert skill.name == "docs-helper"
    assert skill.meta.description == "Helps write docs"
    assert skill.meta.enabled is False
    assert skill.meta.triggers == ["documentation", "README"]
    assert skill.body == "# Instructions\nWrite clear docs."
    assert skill.meta.scope == "project"


def test_parse_skill_file_no_frontmatter(tmp_path):
    path = _write_skill(tmp_path, "bare", "Just a body with no frontmatter.")
    skill = parse_skill_file(path, scope="project")
    assert skill.name == "bare"
    assert skill.meta.description == ""
    assert skill.meta.enabled is True
    assert skill.meta.triggers == []
    assert skill.body == "Just a body with no frontmatter."


def test_parse_skill_file_unclosed_frontmatter(tmp_path):
    path = _write_skill(tmp_path, "bad", "---\nname: bad\nno closing")
    from voidx.skills.registry import SkillParseError
    import pytest
    with pytest.raises(SkillParseError, match="Unclosed frontmatter"):
        parse_skill_file(path, scope="project")


def test_parse_skill_file_quoted_values(tmp_path):
    path = _write_skill(
        tmp_path,
        "quoted",
        "---\nname: quoted\ndescription: 'has: colons, and stuff'\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.meta.description == "has: colons, and stuff"


def test_parse_skill_file_inline_list(tmp_path):
    path = _write_skill(
        tmp_path,
        "inline",
        "---\nname: inline\ntriggers: [alpha, beta]\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.meta.triggers == ["alpha", "beta"]


def test_parse_skill_file_multiline_description(tmp_path):
    path = _write_skill(
        tmp_path,
        "multi",
        '---\nname: multi\ndescription: >\n  This is a long\n  description that spans\n  multiple lines\n---\nbody',
    )
    skill = parse_skill_file(path, scope="project")
    assert "long" in skill.meta.description
    assert "multiple" in skill.meta.description


def test_parse_skill_file_comments_ignored(tmp_path):
    path = _write_skill(
        tmp_path,
        "commented",
        "---\n# this is a comment\nname: commented\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.name == "commented"


def test_parse_skill_file_empty_name_falls_back_to_dir(tmp_path):
    path = _write_skill(
        tmp_path,
        "my-skill",
        "---\nname: ''\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.name == "my-skill"


def test_registry_discovers_global_and_project_with_project_override(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(global_dir, "python", "---\nname: python\n---\nglobal body")
    _write_skill(global_dir, "docs", "---\nname: docs\n---\ndocs body")
    _write_skill(project_dir, "python", "---\nname: python\n---\nproject body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=global_dir,
        project_dir=project_dir,
    )

    skills = registry.discover()

    assert [skill.name for skill in skills] == ["docs", "python"]
    assert registry.get("python").body == "project body"
    assert registry.get("python").meta.scope == "project"
    assert registry.get("docs").meta.scope == "global"


def test_registry_does_not_discover_builtin_workflow_by_default(tmp_path):
    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        global_dir=tmp_path / "global",
        project_dir=tmp_path / "workspace" / ".voidx" / "skills",
    )

    skills = {skill.name: skill for skill in registry.discover()}

    for name in {
        "debug",
        "tdd",
        "verify",
        "feedback",
        "review",
        "plan",
    }:
        assert name not in skills


def test_registry_project_and_global_skills_are_regular_markdown_skills(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(global_dir, "debug", "---\nname: debug\n---\nglobal body")
    _write_skill(project_dir, "debug", "---\nname: debug\n---\nproject body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        global_dir=global_dir,
        project_dir=project_dir,
    )

    skill = registry.get("debug")

    assert skill is not None
    assert skill.body == "project body"
    assert skill.meta.scope == "project"


def test_skill_service_has_no_builtin_workflow_skills(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    assert service.enabled_bundled_skills() == []


def test_skill_service_selects_explicit_refs_only(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "python",
        "---\nname: python\ntriggers: [pytest, pydantic]\n---\nPython rules",
    )
    _write_skill(project_dir, "docs", "---\nname: docs\n---\nDocs rules")
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    explicit = service.select("use $docs for this")
    trigger = service.select("please add pytest coverage")

    assert [match.name for match in explicit] == ["docs"]
    assert explicit[0].reason == "explicit"
    assert trigger == []


def test_skill_body_parse_cache_keeps_activation_dynamic(tmp_path, monkeypatch):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "alpha", "---\nname: alpha\ntriggers: [alpha]\n---\nAlpha rules")
    _write_skill(project_dir, "beta", "---\nname: beta\ntriggers: [beta]\n---\nBeta rules")

    original_read_text = Path.read_text
    calls: list[Path] = []

    def counting_read_text(self, *args, **kwargs):
        calls.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    def service() -> SkillService:
        return SkillService(
            SkillRegistry(
                str(tmp_path / "workspace"),
                bundled_dir=tmp_path / "bundled",
                global_dir=tmp_path / "global",
                project_dir=project_dir,
            )
        )

    first = service().select("use $alpha")
    first_call_count = len(calls)
    second = service().select("use $beta")

    assert [match.name for match in first] == ["alpha"]
    assert [match.name for match in second] == ["beta"]
    assert first_call_count == 2
    assert len(calls) == first_call_count


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
        assert isinstance(node.tools, list)
        assert not hasattr(node, "triggers")
        assert not hasattr(node, "priority")
        assert not hasattr(node, "enabled")
        assert not hasattr(node, "core_rule")
        assert not hasattr(node, "decision_rules")
        assert not hasattr(node, "extra_sections")


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
        "Pick the next task from the plan",
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
    assert "### Persona" in rendered
    assert "### Input" in rendered
    assert "### Output" in rendered
    assert "### Tools" in rendered
    assert "### Internal Subworkflow: TDD Cycle" in rendered
    assert "Exit condition: all plan tasks implemented and broader test set green" in rendered
    assert "### Core Rule" not in rendered
    assert "### Decision Rules" not in rendered


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
    doc = Path(__file__).resolve().parents[2] / "docs" / "archive" / "2026-06-09" / "skill-state-machine-2026-06-08.md"
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


def test_workflow_denied_tools_aggregates_all_active_gates():
    assert workflow_denied_tools(["debug", "tdd"]) >= {"write", "edit"}


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


def test_advance_workflow_states_requires_gate_evidence_before_transition():
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

    assert [run.name for run in states] == ["tdd"]
    assert states[0].status == WorkflowRunStatus.ACTIVE


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



def test_skill_service_available_summaries_exclude_bundled_body(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        ),
        selection=SkillSelectionConfig(auto={"docs"}),
    )

    summaries = service.available_skill_summaries()

    assert summaries == ["- docs [auto]: Write docs"]
    assert "Docs body" not in "\n".join(summaries)


@pytest.mark.asyncio
async def test_instruction_service_system_includes_available_skills_section(tmp_path):
    project_dir = tmp_path / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
    )
    settings = Settings(str(tmp_path))
    settings.set_skill_auto("docs", True)

    instructions = await InstructionService(str(tmp_path), settings=settings).system()

    joined = "\n\n".join(instructions)
    assert "## Available Skills" in joined
    assert "- docs [auto]: Write docs" in joined
    assert "Docs body" not in joined
    assert "debug" not in joined


@pytest.mark.asyncio
async def test_workflow_context_message_renders_fixed_full_workflow_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    context = await InstructionService(str(tmp_path)).workflow_context_for(
        "hello",
        task_intent="general",
    )

    assert context.content.startswith(WORKFLOW_CONTEXT_MARKER)
    assert f"Scope: {WORKFLOW_CONTEXT_SCOPE}" in context.content
    assert "structured workflow definitions" in context.content
    assert "compaction" not in context.content
    for node in WorkflowService().nodes():
        assert f"## Workflow Node: {node.name}" in context.content
        assert f"## Workflow Node Summary: {node.name}" not in context.content


def test_compaction_is_not_a_global_workflow_node():
    assert WorkflowService().get("compaction") is None
    assert "compaction" not in DEFAULT_WORKFLOW_DAG.nodes


def test_compaction_request_contains_runtime_workflow_instructions():
    assert "Preserve durable facts" in COMPACTION_REQUEST
    assert "Remove stale transient execution detail" in COMPACTION_REQUEST
    assert "Write a structured summary only" in COMPACTION_REQUEST
    assert "do not invent facts" in COMPACTION_REQUEST


@pytest.mark.asyncio
async def test_workflow_context_message_expands_all_workflow_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    context = await InstructionService(str(tmp_path)).workflow_context_for(
        "Implement the feature",
        agent="implement",
        task_intent="coding",
        goal_type="feature",
    )

    assert "## Workflow Node: brainstorm" in context.content
    assert "## Workflow Node: tdd" in context.content
    assert "## Workflow Node: verify" in context.content


@pytest.mark.asyncio
async def test_workflow_context_message_stays_fixed_with_active_workflow_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    instruction = InstructionService(str(tmp_path))
    inspect_context = await instruction.workflow_context_for(
        "看看代码",
        agent="voidx",
        task_intent="coding",
        goal_type="inspect",
        workflow_start="brainstorm",
    )
    implement_context = await instruction.workflow_context_for(
        "Implement the feature",
        agent="implement",
        task_intent="coding",
        goal_type="feature",
        workflow_start="tdd",
    )

    assert inspect_context.content == implement_context.content
    assert inspect_context.active != implement_context.active
    assert any("brainstorm" in item for item in inspect_context.active)
    assert any("tdd" in item for item in implement_context.active)


def test_skill_service_respects_disabled_before_enabled(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "docs", "---\nname: docs\nenabled: false\n---\nDocs rules")
    _write_skill(project_dir, "python", "---\nname: python\n---\nPython rules")

    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        ),
        selection=SkillSelectionConfig(enabled={"docs"}, disabled={"python"}),
    )

    assert [skill.name for skill in service.enabled_skills()] == ["docs"]
    assert service.select("$docs")[0].name == "docs"
    assert service.select("$python") == []


def test_registry_discover_caches_results(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "alpha", "---\nname: alpha\n---\nalpha body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=tmp_path / "global",
        project_dir=project_dir,
    )

    first = registry.discover()
    second = registry.discover()
    assert first is second


def test_registry_discover_refreshes_when_skill_file_changes(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    skill_file = _write_skill(project_dir, "alpha", "---\nname: alpha\n---\nalpha body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=tmp_path / "global",
        project_dir=project_dir,
    )

    first = registry.discover()
    skill_file.write_text("---\nname: alpha\n---\nalpha body changed", encoding="utf-8")
    second = registry.discover()

    assert first is not second
    assert second[0].body == "alpha body changed"


def test_registry_invalidate_clears_cache(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "alpha", "---\nname: alpha\n---\nalpha body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=tmp_path / "global",
        project_dir=project_dir,
    )

    first = registry.discover()
    registry.invalidate()
    second = registry.discover()
    assert first is not second
    assert [s.name for s in first] == [s.name for s in second]


def test_skill_service_does_not_implicitly_match_triggers(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "test-skill",
        "---\nname: test-skill\ntriggers: [test]\n---\nTest rules",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    assert service.select("run the latest version") == []
    assert service.select("win the contest") == []
    assert service.select("write a test for this") == []


def test_skill_service_renders_instruction_with_source_path(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    path = _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write documentation\n---\nDocs rules",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    rendered = service.instructions_for("$docs")[0]

    assert f"Path: {path.resolve()}" in rendered
    assert "## Skill: docs" in rendered
    assert "Source: project" in rendered
    assert "Body-Hash:" in rendered
    assert "Description: Write documentation" in rendered
    assert "Docs rules" in rendered


def test_skill_reference_message_wraps_enabled_explicit_refs(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write documentation\n---\nDocs body",
    )

    wrapped = skill_reference_message("use $docs for this", str(tmp_path / "workspace"))

    assert wrapped.remove_spans == [(4, 9)]
    assert wrapped.prefix == "用户指定了技能：\n- docs: Write documentation"
    assert [skill.name for skill in wrapped.skills] == ["docs"]
    assert "Docs body" not in wrapped.prefix


def test_skill_reference_message_ignores_unknown_refs(tmp_path):
    wrapped = skill_reference_message("keep $not-a-skill in text", str(tmp_path / "workspace"))

    assert wrapped.prefix == ""
    assert wrapped.remove_spans == []
    assert wrapped.skills == []


def test_skill_reference_message_uses_provided_service(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".voidx" / "skills"
    _write_skill(project_dir, "docs", "---\nname: docs\ndescription: Write docs\n---\nDocs body")
    service = SkillService(
        SkillRegistry(
            str(workspace),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    def fail_settings(*_args, **_kwargs):
        raise AssertionError("Settings should not be constructed when service is supplied")

    monkeypatch.setattr("voidx.skills.references.Settings", fail_settings)

    wrapped = skill_reference_message("use $docs", str(workspace), service=service)

    assert wrapped.remove_spans == [(4, 9)]
    assert [skill.name for skill in wrapped.skills] == ["docs"]


def test_skill_reference_message_ignores_disabled_refs(tmp_path):
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".voidx" / "skills"
    _write_skill(project_dir, "docs", "---\nname: docs\ndescription: Write docs\n---\nDocs body")
    settings = Settings(str(workspace))
    settings.set_skill_enabled("docs", False)

    wrapped = skill_reference_message("use $docs for this", str(workspace), settings=settings)

    assert wrapped.prefix == ""
    assert wrapped.remove_spans == []


def test_list_skill_candidates_accepts_prebuilt_service(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".voidx" / "skills"
    _write_skill(project_dir, "docs", "---\nname: docs\ndescription: Write docs\n---\nDocs body")
    service = SkillService(
        SkillRegistry(
            str(workspace),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    def fail_settings(*_args, **_kwargs):
        raise AssertionError("Settings should not be constructed when service is supplied")

    monkeypatch.setattr("voidx.ui.tools.skill_picker.Settings", fail_settings)

    candidates = list_skill_candidates(str(workspace), "do", service=service)

    assert [candidate.name for candidate in candidates] == ["docs"]
