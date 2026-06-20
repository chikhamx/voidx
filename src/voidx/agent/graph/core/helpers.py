from __future__ import annotations

from voidx.agent.agents import AgentDef
from voidx.agent.runtime_context import COMPACTION_GUIDE_MARKER, InteractionMode
from voidx.agent.task_state import GoalResolution, TaskState
from voidx.workflow import workflow_personas
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus


def _is_context_overflow_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        pattern in msg
        for pattern in (
            "context_length_exceeded",
            "context length",
            "too many tokens",
            "maximum context",
            "token limit",
            "input is too long",
            "request too large",
            "context window",
        )
    )


def _render_inline_compaction_guide(*, tail_anchor_id: str, head_count: int, previous_summary: str) -> str:
    previous = previous_summary.strip() or "(none)"
    return (
        f"{COMPACTION_GUIDE_MARKER}\n"
        "Scope: inline-context-compaction\n\n"
        "The conversation is large enough to compact older context without a separate compaction request.\n"
        "If you can preserve the durable facts now, call compact before continuing.\n\n"
        "Rules:\n"
        "- Summarize only older context before the tail anchor.\n"
        "- Preserve durable facts, decisions, constraints, changed files, verification results, blockers, and next steps.\n"
        "- Drop transient narration, repeated tool outputs, and stale execution detail.\n"
        "- Do not answer the user through compact; use it only to update runtime memory.\n"
        "- After compact succeeds, continue with the user's request normally.\n\n"
        "Current compaction request:\n"
        f"- tail_anchor_id: {tail_anchor_id}\n"
        f"- older_messages_to_summarize: {head_count}\n"
        f"- previous_summary:\n{previous}"
    )


def _merge_workflow_runs(*groups: list[WorkflowRunState | dict]) -> list[WorkflowRunState]:
    merged: dict[str, WorkflowRunState] = {}
    for group in groups:
        for item in group:
            try:
                run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            except ValueError:
                continue
            merged[run.name] = run
    return list(merged.values())


def _workflow_names(group: list[WorkflowRunState | dict]) -> list[str]:
    names: list[str] = []
    for item in group:
        if isinstance(item, WorkflowRunState):
            name = item.name
        elif isinstance(item, dict):
            name = item.get("name", "")
        else:
            name = ""
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _persona_for_workflow_runs(
    group: list[WorkflowRunState | dict],
    *,
    fallback: str = "coordinate",
) -> str:
    personas: list[str] = []
    for item in group:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        personas.extend(persona.strip() for persona in run.personas if persona.strip())
    if not personas:
        return fallback or "coordinate"
    return ",".join(dict.fromkeys(personas))


def _persona_for_child_workflow(group: list[WorkflowRunState | dict], join: str) -> str:
    persona = _persona_for_workflow_runs(group, fallback="")
    if persona:
        return persona
    personas = [item for item in workflow_personas(join) if item.strip()]
    return ",".join(dict.fromkeys(personas)) or "explore"


def _interaction_mode_for_persona(persona: str) -> str:
    personas = {item.strip() for item in persona.split(",") if item.strip()}
    return InteractionMode.PLAN.value if "plan" in personas else InteractionMode.AUTO.value




def _invalidate_tui(host: object) -> None:
    app = getattr(host, "_app", None)
    invalidate = getattr(app, "invalidate", None)
    if callable(invalidate):
        invalidate()


def _agent_static_tool_defs(agent: AgentDef | None, all_tool_defs: list[dict]) -> list[dict]:
    """Apply AgentDef's static tool catalog visibility.

    This is not runtime persona/workflow policy. Runtime policy is enforced by
    the tool-engine during authorization; this only prevents tools outside the
    current agent identity's declared catalog from being advertised to the LLM.
    """
    if agent is None:
        return all_tool_defs
    agent_tool_ids = set(agent.tools)
    mcp_allowed = bool(agent.mcp_tools)
    return [
        tool_def
        for tool_def in all_tool_defs
        if (
            tool_def["function"]["name"] in agent_tool_ids
            or (mcp_allowed and tool_def["function"]["name"].startswith("mcp__"))
        )
    ]


def _task_state_for_context(value: object, fallback: TaskState | None = None) -> TaskState:
    if isinstance(value, TaskState):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        try:
            return TaskState.model_validate(value)
        except ValueError:
            pass
    if fallback is not None:
        return fallback.model_copy(deep=True)
    return TaskState()
