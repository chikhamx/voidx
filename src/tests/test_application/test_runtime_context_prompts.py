import sys
from pathlib import Path


from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.application.prompts import BASE_SYSTEM, persona_prompt, workflow_runtime
from voidx.agent.application.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.agent.domain.task.state import TaskState
from voidx.config import Config


def test_runtime_context_builder_uses_structured_prompt_sections(tmp_path):
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt=BASE_SYSTEM,
        workflow_runtime=workflow_runtime(DEFAULT_WORKFLOW_DAG),
        persona_prompt=persona_prompt(),
        persona="coordinate",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(),
    ).build()

    section_names = context.section_names()

    assert "Agent Role" not in section_names
    assert "Tool Contract" not in section_names
    assert "Base System" in section_names
    assert "Persona" in section_names
    assert "Workflow Runtime" in section_names
    assert "Runtime State" in section_names
    assert "Workspace Facts" not in section_names
    assert "Current Task State" in section_names

    system = context.render_system()
    assert "You are voidx, an autonomous coding agent." in system
    assert "## Persona Model" in system
    assert "## Workflow Runtime" in system
    assert "## Runtime State" in system
    assert "## Workspace Facts" not in system
    assert "## Tool Contract" not in system
