import sys
from pathlib import Path


import pytest

from voidx.llm.compaction import COMPACTION_REQUEST
from voidx.agent.application.instruction import InstructionService
from voidx.config import Settings
from voidx.skills.registry import SkillRegistry, parse_skill_file
from voidx.agent.application.automation.workflow.context import WORKFLOW_CONTEXT_MARKER, WORKFLOW_CONTEXT_SCOPE
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.domain.automation.workflow_policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_exit_summaries,
    workflow_terminal_condition,
    workflow_transitions,
)
from voidx.agent.application.automation.workflow.runtime import (
    WorkflowActivationSource,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    advance_workflow_states,
)
from voidx.skills.schema import SkillSelectionConfig
from voidx.skills.application.resolve_references import ResolveSkillReferences
from voidx.skills.service import SkillService
from voidx.presentation.tools.skill_picker import list_skill_candidates
from voidx.agent.application.automation.workflow.service import WorkflowService
from tests.test_skills.conftest import _write_skill






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

    service = SkillService(
        SkillRegistry(str(tmp_path)),
        selection=settings.get_skill_selection(),
    )
    instructions = await InstructionService(
        str(tmp_path),
        settings=settings,
        skill_summaries_provider=service.available_skill_summaries,
    ).system()

    joined = "\n\n".join(instructions)
    assert "## Available Skills" in joined
    assert "- docs [auto]: Write docs" in joined
    assert "Docs body" not in joined
    assert "debug" not in joined


@pytest.mark.asyncio
async def test_workflow_context_message_renders_fixed_full_workflow_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    context = await InstructionService(str(tmp_path)).workflow_context_for(workflow_dag=DEFAULT_WORKFLOW_DAG)

    assert context.content.startswith(WORKFLOW_CONTEXT_MARKER)
    assert f"Scope: {WORKFLOW_CONTEXT_SCOPE}" in context.content
    assert "structured workflow definitions" in context.content
    assert "compaction" not in context.content
    for node in WorkflowService(DEFAULT_WORKFLOW_DAG).nodes():
        assert f"## Workflow Node: {node.name}" in context.content
        assert f"## Workflow Node Summary: {node.name}" not in context.content


def test_compaction_is_not_a_global_workflow_node():
    assert WorkflowService(DEFAULT_WORKFLOW_DAG).get("compaction") is None
    assert "compaction" not in DEFAULT_WORKFLOW_DAG.nodes


def test_compaction_request_contains_runtime_workflow_instructions():
    assert "Preserve durable facts" in COMPACTION_REQUEST
    assert COMPACTION_REQUEST.lower().count("durable facts") == 1
    assert "Remove stale transient execution detail" in COMPACTION_REQUEST
    assert "Write a structured summary only" in COMPACTION_REQUEST
    assert "do not invent facts" in COMPACTION_REQUEST


@pytest.mark.asyncio
async def test_workflow_context_message_expands_all_workflow_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    context = await InstructionService(str(tmp_path)).workflow_context_for(
        workflow_dag=DEFAULT_WORKFLOW_DAG,
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
        workflow_dag=DEFAULT_WORKFLOW_DAG,
        goal_type="inspect",
        workflow_start="brainstorm",
    )
    implement_context = await instruction.workflow_context_for(
        workflow_dag=DEFAULT_WORKFLOW_DAG,
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


def test_resolve_skill_references_wraps_enabled_explicit_refs(tmp_path):
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write documentation\n---\nDocs body",
    )
    service = SkillService(
        SkillRegistry(
            str(workspace),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    wrapped = ResolveSkillReferences(service)("use $docs for this")

    assert wrapped.remove_spans == [(4, 9)]
    assert wrapped.prefix == (
        "Explicit skills requested:\n"
        "- docs: Write documentation\n\n"
        "Before acting, call skill with op='load' for each listed skill. "
        "Descriptions are index metadata, not the full instructions."
    )
    assert [skill.name for skill in wrapped.skills] == ["docs"]
    assert "Docs body" not in wrapped.prefix


def test_resolve_skill_references_ignores_unknown_refs(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "project",
        )
    )

    wrapped = ResolveSkillReferences(service)("keep $not-a-skill in text")

    assert wrapped.prefix == ""
    assert wrapped.remove_spans == []
    assert wrapped.skills == []


def test_resolve_skill_references_ignores_disabled_refs(tmp_path):
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".voidx" / "skills"
    _write_skill(project_dir, "docs", "---\nname: docs\ndescription: Write docs\n---\nDocs body")
    service = SkillService(
        SkillRegistry(
            str(workspace),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        ),
        selection=SkillSelectionConfig(disabled={"docs"}),
    )

    wrapped = ResolveSkillReferences(service)("use $docs for this")

    assert wrapped.prefix == ""
    assert wrapped.remove_spans == []


def test_list_skill_candidates_requires_prebuilt_service(tmp_path):
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

    candidates = list_skill_candidates("do", service=service)

    assert [candidate.name for candidate in candidates] == ["docs"]
