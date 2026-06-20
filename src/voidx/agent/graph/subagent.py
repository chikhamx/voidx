"""Child agent execution loop."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.agents import (
    CHILD_RUN_CONSTRAINTS,
    PLAN_MODE_APPEND,
    AgentDef,
    child_run_agent_def,
)
from voidx.agent.prompts import BASE_SYSTEM, WORKFLOW_RUNTIME, persona_prompt
from voidx.agent.graph.runtime_guards import (
    RuntimeGuardState,
    WallClockGuardState,
    build_failure_key,
    cycle_summary_from_tools,
)
from voidx.agent.graph.streaming import extract_text, stream_llm
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.agent.todo_state import todo_run_state_from_result
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
)
from voidx.runtime.task_state import GoalResolution, TaskState, WorkflowRoute
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.config import Config
from voidx.llm.service import create_chat_model, resolve_protocol
from voidx.llm.instruction import WorkflowRuntimeContext
from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.service import save_context_frame_from_messages
from voidx.memory.subagents import append_subagent_event
from voidx.runtime.ui import CaptureConsole, OutputTree, StreamingRenderer
from voidx.tools.service import ToolContext, ToolRegistry, TaskTracker
from voidx.runtime.ui_port import AgentUiPort, runtime_ui_port


_SAFETY_STEP_LIMIT = 50

async def run_subagent(
    agent_def: AgentDef,
    task_description: str,
    model_override: str | None,
    api_key: str,
    config: Config,
    tracker: TaskTracker | None = None,
    runtime_persona: str | None = None,
    *,
    goal_resolution: GoalResolution,
    result_contract,
    run_metadata: dict[str, object] | None = None,
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
    agent_def = child_run_agent_def(agent_def)
    persona = (runtime_persona or "explore").strip() or "explore"
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

    messages = [HumanMessage(content=_task_payload(task_description, result_contract))]

    context_config = config.model_copy(deep=True)
    context_config.model = model_cfg
    interaction_mode = InteractionMode.PLAN.value if persona == "plan" else InteractionMode.AUTO.value
    mode_prompt = PLAN_MODE_APPEND if InteractionMode.parse(interaction_mode) == InteractionMode.PLAN else ""
    workflow_context = workflow_runtime_context or WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])
    context_cache = ContextCompilerCache()
    plan = goal_resolution.plan
    sub_task_state = TaskState(
        current_intent=goal_resolution.intent.type,
        current_goal=goal_resolution.goal,
        workflow_route=WorkflowRoute(join=plan.join, leave=plan.leave) if plan is not None else None,
        workflow_runs={run.name: run for run in workflow_context.runs},
    )
    guard_state = RuntimeGuardState(wall_clock=WallClockGuardState.for_subagent())
    pending_guard_guidance: list[str] = []

    context, context_cache = RuntimeContextBuilder(
        config=context_config,
        workspace=config.workspace,
        base_system_prompt=BASE_SYSTEM,
        workflow_runtime=WORKFLOW_RUNTIME,
        persona_prompt=persona_prompt(),
        runtime_constraints=CHILD_RUN_CONSTRAINTS,
        mode_prompt=mode_prompt,
        persona=persona,
        interaction_mode=interaction_mode,
        workflow_runs=workflow_context.runs,
        active_workflow_summaries=workflow_context.active,
        task_state=sub_task_state,
    ).build_incremental(context_cache)
    context.apply_to_messages(messages)

    ctx = ToolContext(
        workspace=config.workspace,
        session_id=session_id or "default",
        lsp_manager=lsp_manager,
        sandbox_mode=config.sandbox_mode.value,
        sandbox_extra_paths=config.sandbox_workspace_write,
    )

    # Register with tracker
    task_id = f"sub_{agent_def.name}_{persona}_{int(time.time())}"
    if tracker:
        tracker.start(task_id, persona, task_description)

    def mark_finished(reason: str) -> None:
        if run_metadata is not None:
            run_metadata.update({
                "finish_reason": reason,
            })


    def drain_guard_guidance() -> list[HumanMessage]:
        messages: list[HumanMessage] = []
        while pending_guard_guidance:
            text = pending_guard_guidance.pop(0)
            messages.append(HumanMessage(content=text))
        return messages

    try:
        step = 0
        while step < _SAFETY_STEP_LIMIT:
            step += 1

            if capture_tree and parent_node is not None:
                capture = CaptureConsole(capture_tree, parent_node, agent_id=agent_id)
                capture.step_header(persona)
            else:
                ui_port.ui.step_header(persona)

            llm_messages = [*messages, *drain_guard_guidance()]
            model_with_tools = model.bind_tools(tool_defs) if tool_defs else model
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
                        "tool_count": len(tool_defs),
                        "agent_id": agent_id,
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
            if session_id:
                tool_calls = getattr(assistant_msg, "tool_calls", None) or []
                tool_refs = [
                    {"name": tc.get("name", ""), "id": tc.get("id", "")}
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]
                await append_subagent_event(session_id, f"agent_{agent_id}", {
                    "type": "assistant_message",
                    "step": step,
                    "content_preview": (assistant_msg.content or "")[:200],
                    "tool_call_refs": tool_refs,
                })

            if not assistant_msg.tool_calls:
                text = extract_text(assistant_msg)
                if tracker:
                    tracker.update(task_id, last_output=text[:200])
                    tracker.finish(task_id, "completed")
                mark_finished("final_answer")
                return text

            # Update tracker with preview
            text_preview = extract_text(assistant_msg)[:200]
            if tracker and text_preview:
                tracker.update(task_id, last_output=text_preview)

            repetitive_decision = guard_state.repetitive_tools.decision_for_pending(list(assistant_msg.tool_calls))
            if repetitive_decision.action in {"skip", "terminate"}:
                guard_tool_msgs = [
                    ToolMessage(
                        content=sanitize_tool_message_content(repetitive_decision.message, workspace=ctx.workspace),
                        tool_call_id=tc.get("id", ""),
                    )
                    for tc in assistant_msg.tool_calls
                ]
                messages.extend(guard_tool_msgs)
                sub_messages.extend(guard_tool_msgs)
                if repetitive_decision.action == "terminate":
                    if tracker:
                        tracker.update(task_id, last_output=repetitive_decision.message[:200])
                        tracker.finish(task_id, "completed")
                    mark_finished("guard_terminated")
                    return repetitive_decision.message
                continue

            if authorize_tools:
                approved, denied = await authorize_tools(assistant_msg.tool_calls)
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
                if session_id:
                    await append_subagent_event(session_id, f"agent_{agent_id}", {
                        "type": "tool_result",
                        "step": step,
                        "tool_name": tid,
                        "tool_call_id": cid,
                        "args": targs,
                        "content": result.output,
                        "summary": result.summary,
                        "ok": True,
                    })
                if capture_tree and parent_node is not None:
                    capture.tool_done(tid, 0.0, True, tool_call_id=cid)
                    capture.tool_result(result.output, tool_call_id=cid)
                return {
                    "tool_message": ToolMessage(
                        content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
                        tool_call_id=cid,
                    ),
                    "result": result,
                    "tool_call": tc,
                    "todo_state": todo_state,
                }

            executed = await asyncio.gather(*[run_one(tc) for tc in approved])
            executed = [item for item in executed if item is not None]
            denied_msgs = [
                ToolMessage(
                    content=sanitize_tool_message_content(reason, workspace=ctx.workspace),
                    tool_call_id=tc.get("id", ""),
                )
                for tc, reason in denied
            ]
            tool_msgs = [item["tool_message"] for item in executed]
            messages.extend(tool_msgs + denied_msgs)
            sub_messages.extend(tool_msgs + denied_msgs)

            def result_ok(result) -> bool:
                metadata = getattr(result, "metadata", {}) or {}
                if metadata.get("error") or metadata.get("blocked") or metadata.get("timeout"):
                    return False
                if "exit_code" in metadata:
                    try:
                        return int(metadata.get("exit_code") or 0) == 0
                    except (TypeError, ValueError):
                        return False
                return True

            for item in executed:
                metadata = getattr(item["result"], "metadata", {}) or {}
                if metadata.get("runtime_guard"):
                    continue
                if result_ok(item["result"]):
                    guard_state.tool_failures.record_success(item["tool_call"])
                    continue
                key = build_failure_key(item["tool_call"], item["result"])
                guidance = guard_state.tool_failures.record_failure(
                    key,
                    str(getattr(item["result"], "summary", "") or getattr(item["result"], "output", ""))[:500],
                )
                if guidance is not None:
                    pending_guard_guidance.append(guidance.message)

            next_todo_state = sub_task_state.todo_state
            for item in executed:
                if item["todo_state"] is not None:
                    next_todo_state = item["todo_state"]
            summary = cycle_summary_from_tools(
                executed,
                previous_todo_state=sub_task_state.todo_state,
                next_todo_state=next_todo_state,
                workflow_changed=False,
                result_ok=result_ok,
            )
            sub_task_state.todo_state = next_todo_state
            guidance = guard_state.repetitive_tools.record_cycle(summary)
            if guidance is not None:
                pending_guard_guidance.append(guidance.message)
            guidance = guard_state.no_progress.record_cycle(summary)
            if guidance is not None:
                pending_guard_guidance.append(guidance.message)
            no_progress_decision = guard_state.no_progress.decision()
            if no_progress_decision.action == "terminate":
                if tracker:
                    tracker.update(task_id, last_output=no_progress_decision.message[:200])
                    tracker.finish(task_id, "completed")
                mark_finished("guard_terminated")
                return no_progress_decision.message
            wall_clock_decision = guard_state.wall_clock.record_check(
                label=agent_def.name or persona,
                latest_action=summary.only_tool or ", ".join(summary.tool_names[:3]),
            )
            if wall_clock_decision.action == "terminate":
                if tracker:
                    tracker.update(task_id, last_output=wall_clock_decision.message[:200])
                    tracker.finish(task_id, "completed")
                mark_finished("guard_terminated")
                return wall_clock_decision.message

        if tracker:
            tracker.finish(task_id, "completed")
        mark_finished("safety_limit")
        return extract_text(messages[-1]) if messages else "Safety step limit reached."

    except Exception as e:
        if tracker:
            tracker.update(task_id, last_output=str(e)[:200])
            tracker.finish(task_id, "error")
        mark_finished("error")
        raise




def _task_payload(task_description: str, result_contract) -> str:
    schema_name = str(getattr(result_contract, "schema_name", "") or "agent_result")
    result_format = str(getattr(result_contract, "format", "") or "").strip()
    if not result_format:
        return task_description
    return (
        f"{task_description}\n\n"
        "Result contract:\n"
        f"- schema_name: {schema_name}\n"
        f"- format: {result_format}\n"
        "Return the final answer using this contract."
    )
