from __future__ import annotations

from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG

from types import SimpleNamespace

from voidx.agent.application.prompts import (
    BASE_SYSTEM,
    CHAT_PROFILE_SPEC,
    workflow_runtime,
    assemble_base_system,
    persona_prompt,
)
from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.automation.loop import LOOP_PROFILE, LoopSpec, loop_profile_for_spec
from voidx.agent.domain.prompt_policy import (
    ChatPromptPolicy,
    CodingPromptPolicy,
    GoalPromptPolicy,
    LoopPromptPolicy,
)
from voidx.agent.domain.task.state import TaskState
from voidx.config import Config

from .snapshot import assert_snapshot


def _profile(name: str, policy, context, *, base_system=BASE_SYSTEM) -> dict[str, object]:
    sections = policy.profile_sections(context)
    built = RuntimeContextBuilder(
        config=Config(workspace="${WORKSPACE}"),
        workspace="${WORKSPACE}",
        base_system_prompt=base_system,
        workflow_runtime=workflow_runtime(DEFAULT_WORKFLOW_DAG),
        persona_prompt=persona_prompt(),
        persona="coordinate",
        interaction_mode="auto",
        task_state=TaskState(),
        session_date="2026-08-05 UTC",
        profile_sections=sections,
        suppress_sections=policy.suppress_sections(),
    ).build()
    ordered = [*built.sections, *built.task_sections]
    return {
        "name": name,
        "sections": [section.model_dump(mode="json") for section in ordered],
        "rendered": built.render_system(),
    }


def test_prompt_contract(monkeypatch) -> None:
    from pathlib import Path
    import voidx.platform.paths as paths

    monkeypatch.setattr(paths, "voidx_home", lambda: Path("/${HOME}/.voidx"))
    chat = ChatPromptPolicy()
    loop_work_profile = loop_profile_for_spec(LoopSpec(prompt="Inspect ${WORKSPACE}", generation="fixed"))
    profiles = [
        _profile("coding", CodingPromptPolicy(), None),
        _profile(
            "chat",
            chat,
            None,
            base_system=assemble_base_system(CHAT_PROFILE_SPEC, available_tools=None),
        ),
        _profile("goal_intake", GoalPromptPolicy(), SimpleNamespace(goal_phase="intake", runtime_profile=GOAL_PROFILE)),
        _profile("goal_evaluator", GoalPromptPolicy(), SimpleNamespace(goal_phase="evaluator", runtime_profile=GOAL_PROFILE)),
        _profile("loop_idle", LoopPromptPolicy(), SimpleNamespace(loop_phase="idle", runtime_profile=LOOP_PROFILE)),
        _profile("loop_work", LoopPromptPolicy(), SimpleNamespace(loop_phase="work", runtime_profile=loop_work_profile)),
        _profile("subagent", CodingPromptPolicy(), None),
    ]
    assert_snapshot("prompts.json", profiles)
