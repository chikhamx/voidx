import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.prompts import BASE_SYSTEM, WORKFLOW_RUNTIME, persona_prompt
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.agent.task_state import TaskState
from voidx.config import Config


def test_runtime_context_builder_uses_structured_prompt_sections(tmp_path):
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt=BASE_SYSTEM,
        workflow_runtime=WORKFLOW_RUNTIME,
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
