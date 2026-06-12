"""Child agent execution loop."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.agents import BASE_SYSTEM_PROMPT, PLAN_MODE_APPEND, SUB_VOIDX_PROMPT, AgentDef
from voidx.agent.graph.convergence import (
    build_convergence_messages,
    generate_fallback_summary,
)
from voidx.agent.graph.streaming import extract_text, stream_llm
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.agent.todo_state import todo_run_state_from_result
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
)
from voidx.runtime.task_state import Goal, GoalType, TaskIntent, TaskState
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.config import Config
from voidx.llm.provider import create_chat_model, resolve_protocol
from voidx.llm.instruction import WorkflowRuntimeContext
from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
from voidx.tools.task_tracker import TaskTracker
from voidx.runtime.ui_port import AgentUiPort, runtime_ui_port
from voidx.ui.output.capture import CaptureConsole
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.tree import OutputTree


async def run_subagent(
    agent_def: AgentDef,
    task_description: str,
    model_override: str | None,
    api_key: str,
    config: Config,
    tracker: TaskTracker | None = None,
    runtime_persona: str | None = None,
    capture_tree: OutputTree | None = None,
    parent_node=None,
    sub_messages: list | None = None,
    authorize_tools=None,
    debug: bool = True,
    agent_id: int = -1,
    session_id: str | None = None,
    usage_stats: UsageStats | None = None,
    lsp_manager=None,
    parent_tools: ToolRegistry | None = None,
    workflow_runtime_context: WorkflowRuntimeContext | None = None,
    todo_state_sink=None,
    ui_port: AgentUiPort = runtime_ui_port,
) -> str:
    """Run a child agent in its own message context."""
    persona = (runtime_persona or agent_def.name).strip() or "explore"
    model_cfg = config.model.model_copy()
    if model_override:
        model_cfg.model = model_override
    elif agent_def.model:
        model_cfg.model = agent_def.model

    # Child agents get a filtered view of the parent registry so dynamic MCP
    # wrappers can be reused when an agent explicitly opts in.
    allowed_ids = set(agent_def.tools)
    if not agent_def.can_delegate:
        allowed_ids.discard("agent")
    base_tools = parent_tools or ToolRegistry()
    if agent_def.mcp_tools and parent_tools is not None:
        allowed_ids.update(tid for tid in parent_tools.ids() if tid.startswith("mcp__"))
    agent_tools = base_tools.filtered_copy(allowed_ids)

    model = create_chat_model(api_key, model_cfg)
    tool_defs = agent_tools.tools_for_llm()
    tool_defs = filter_unavailable_lsp_tools(tool_defs, lsp_manager)

    if sub_messages is None:
        sub_messages = []

    messages = [HumanMessage(content=task_description)]

    context_config = config.model_copy(deep=True)
    context_config.model = model_cfg
    interaction_mode = InteractionMode.PLAN.value if persona == "plan" else InteractionMode.AUTO.value
    mode_prompt = PLAN_MODE_APPEND if InteractionMode.parse(interaction_mode) == InteractionMode.PLAN else ""
    task_intent = _task_intent_for_agent(persona)
    goal_type = _goal_type_for_agent(persona, task_description)
    workflow_context = workflow_runtime_context or WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])
    context_cache = ContextCompilerCache()

    sub_goal = None
    if goal_type:
        sub_goal = Goal(
            type=GoalType(goal_type),
            target=task_description,
            expected_result="",
            user_requested_write=agent_def.can_write,
            needs_confirmation=False,
        )
    sub_task_state = TaskState(
        current_intent=TaskIntent(task_intent),
        current_goal=sub_goal,
    )

    context, context_cache = RuntimeContextBuilder(
        config=context_config,
        workspace=config.workspace,
        base_system_prompt=BASE_SYSTEM_PROMPT,
        persona_prompt=_agent_prompt(agent_def),
        mode_prompt=mode_prompt,
        tool_contract=agent_def.tool_contract,
        persona=persona,
        interaction_mode=interaction_mode,
        workflow_context_content=workflow_context.content,
        workflow_runs=workflow_context.runs,
        active_workflow_summaries=workflow_context.active,
        current_user_text=task_description,
        task_state=sub_task_state,
    ).build_incremental(context_cache)
    context.apply_to_messages(messages)

    ctx = ToolContext(
        workspace=config.workspace,
        lsp_manager=lsp_manager,
        sandbox_mode=config.sandbox_mode.value,
        sandbox_extra_paths=config.sandbox_workspace_write,
    )

    # Register with tracker
    task_id = f"sub_{agent_def.name}_{persona}_{int(time.time())}"
    if tracker:
        tracker.start(task_id, persona, task_description, agent_def.max_steps)

    try:
        for step in range(1, agent_def.max_steps + 1):
            if tracker:
                tracker.update(task_id, step=step)

            if capture_tree and parent_node is not None:
                capture = CaptureConsole(capture_tree, parent_node, agent_id=agent_id)
                capture.step_header(step, agent_def.max_steps, persona)
            else:
                ui_port.ui.step_header(step, agent_def.max_steps, persona)

            has_tool_budget = step < agent_def.max_steps - 1
            active_tool_defs = tool_defs if has_tool_budget else []
            convergence_messages, convergence_forced = build_convergence_messages(
                step=step,
                max_steps=agent_def.max_steps,
                has_tool_budget=has_tool_budget,
                goal=task_description,
            )
            llm_messages = [*messages, *convergence_messages]
            model_with_tools = model.bind_tools(active_tool_defs) if active_tool_defs else model
            renderer = StreamingRenderer(ui_port.console, debug=debug, agent_id=agent_id, headless=True)
            context_tokens = estimate_context_tokens(llm_messages, config.model.model)
            if usage_stats is not None:
                usage_stats.update_context(context_tokens)
            if session_id:
                await save_context_frame_from_messages(
                    session_id=session_id,
                    frame_kind="worker",
                    agent_persona=persona,
                    provider=config.model.provider,
                    model=config.model.model,
                    messages=llm_messages,
                    token_estimate=context_tokens,
                    metadata={
                        "step": step,
                        "max_steps": agent_def.max_steps,
                        "tool_count": len(active_tool_defs),
                        "agent_id": agent_id,
                        "convergence_hint_count": len(convergence_messages),
                        "convergence_forced": convergence_forced,
                    },
                )
            assistant_msg = await stream_llm(
                model_with_tools,
                llm_messages,
                renderer,
                resolve_protocol(config.model),
            )
            if usage_stats is not None:
                usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, config.model.model),
                    messages=llm_messages,
                    model=config.model.model,
                    cache_key=f"{config.model.provider}/{config.model.model}",
                )
            messages.append(assistant_msg)
            sub_messages.append(assistant_msg)

            if not has_tool_budget and assistant_msg.tool_calls:
                text = generate_fallback_summary({
                    "messages": messages,
                    "goal": task_description,
                    "tool_results": {},
                    "step_count": step,
                    "max_steps": agent_def.max_steps,
                })
                if tracker:
                    tracker.update(task_id, last_output=text[:200])
                    tracker.finish(task_id, "completed")
                return text

            if not assistant_msg.tool_calls:
                text = extract_text(assistant_msg)
                if convergence_forced and len(text.strip()) < 20:
                    text = generate_fallback_summary({
                        "messages": messages,
                        "goal": task_description,
                        "tool_results": {},
                        "step_count": step,
                        "max_steps": agent_def.max_steps,
                    })
                if tracker:
                    tracker.update(task_id, last_output=text[:200])
                    tracker.finish(task_id, "completed")
                return text

            # Update tracker with preview
            text_preview = extract_text(assistant_msg)[:200]
            if tracker and text_preview:
                tracker.update(task_id, last_output=text_preview)

            if authorize_tools:
                approved, denied = await authorize_tools(assistant_msg.tool_calls, agent_def.name)
            else:
                approved = list(assistant_msg.tool_calls)
                denied = []

            async def run_one(tc):
                tid = tc.get("name", "")
                targs = tc.get("args", {})
                cid = tc.get("id", "")
                if capture_tree and parent_node is not None:
                    capture.tool_call(tid, targs, tool_call_id=cid)
                result = await agent_tools.execute_tool(tid, targs, ctx)
                todo_state = todo_run_state_from_result(result) if tid == "todo" else None
                if todo_state_sink is not None and todo_state is not None and todo_state.items:
                    todo_state_sink(todo_state)
                if ui_port.via_events() and tid == "todo":
                    todo_event = todo_updated_event(result, agent_id=agent_id)
                    if todo_event is not None:
                        ui_port.events.emit_direct(todo_event)
                if capture_tree and parent_node is not None:
                    capture.tool_done(tid, 0.0, True, tool_call_id=cid)
                    capture.tool_result(result.output, tool_call_id=cid)
                if tid == "todo":
                    return None
                return ToolMessage(
                    content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
                    tool_call_id=cid,
                )

            tool_msgs = await asyncio.gather(*[run_one(tc) for tc in approved])
            tool_msgs = [msg for msg in tool_msgs if msg is not None]
            denied_msgs = [
                ToolMessage(
                    content=sanitize_tool_message_content(reason, workspace=ctx.workspace),
                    tool_call_id=tc.get("id", ""),
                )
                for tc, reason in denied
            ]
            messages.extend(tool_msgs + denied_msgs)
            sub_messages.extend(tool_msgs + denied_msgs)

        if tracker:
            tracker.finish(task_id, "completed")
        return extract_text(messages[-1]) if messages else "Max steps reached."

    except Exception as e:
        if tracker:
            tracker.update(task_id, last_output=str(e)[:200])
            tracker.finish(task_id, "error")
        raise


def _task_intent_for_agent(agent_name: str) -> str:
    return "coding" if agent_name in {"implement", "review", "plan", "explore"} else "general"


def _goal_type_for_agent(agent_name: str, task_description: str = "") -> str:
    if agent_name == "review":
        return "review"
    if agent_name == "plan":
        return "design"
    if agent_name == "implement":
        lowered = task_description.lower()
        if "bug" in lowered or "fix" in lowered or "failed" in lowered or "failure" in lowered:
            return "bugfix"
        return "feature"
    if agent_name == "explore":
        return "inspect"
    return ""


def _agent_prompt(agent_def: AgentDef) -> str:
    try:
        return agent_def.persona_prompt
    except ValueError:
        return SUB_VOIDX_PROMPT
