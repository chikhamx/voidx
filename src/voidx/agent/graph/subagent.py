"""Child agent execution loop."""

from __future__ import annotations

import asyncio
import re
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.agents import AgentDef, child_run_agent_def
from voidx.agent.prompts import WORKFLOW_RUNTIME, build_base_system, persona_prompt
from voidx.agent.graph.runtime_guards import (
    RuntimeGuardState,
    WallClockGuardState,
    build_failure_key,
    cycle_summary_from_tools,
)
from voidx.agent.graph.streaming import extract_text, stream_llm
from voidx.agent.graph.core.helpers import LLMErrorKind, _classify_llm_error, _LLM_MAX_RETRIES, _llm_retry_delay, _clean_error_message
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.agent.todo_state import todo_run_state_from_result
from voidx.runtime.intent import PersonaName
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
)
from voidx.runtime.task_state import GoalResolution, TaskState, WorkflowRoute
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.agent.tool_result_storage import maybe_persist_tool_result
from voidx.agent.tool_filters import filter_unavailable_lsp_tools, strip_gemini_unsupported_schema_keys
from voidx.config import Config
from voidx.llm.service import create_chat_model, resolve_protocol
from voidx.llm.instruction import WorkflowRuntimeContext
from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens_with_tools,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.service import save_context_frame_from_messages
from voidx.memory.subagents import append_subagent_event
from voidx.runtime.ui import (
    CaptureConsole,
    OutputTree,
    StatusFinished,
    StatusUpdated,
    StreamingRenderer,
)
from voidx.tools.service import ToolContext, ToolRegistry, TaskTracker
from voidx.runtime.ui_port import AgentUiPort, runtime_ui_port


_SAFETY_STEP_LIMIT = 50
_RESULT_CONTRACT_RETRY_LIMIT = 2
_BLOCKED_CHILD_TOOLS = {"agent", "clarify", "checkpoint", "schedule_wakeup"}

async def run_subagent(
    agent_def: AgentDef,
    task_description: str,
    api_key: str | None,
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
    permission_snapshot=None,
    ui_port: AgentUiPort = runtime_ui_port,
) -> str:
    """Run a child agent in its own message context."""
    agent_def = child_run_agent_def(agent_def)
    persona = (runtime_persona or PersonaName.EXPLORE).strip() or PersonaName.EXPLORE
    model_cfg = config.model.model_copy()
    if agent_def.model:
        model_cfg.model = agent_def.model

    # Child agents inherit the full parent tool registry.
    # Access control is handled by the permission layer and workflow denied_tools.
    agent_tools = parent_tools or ToolRegistry()
    blocked_child_tools = _BLOCKED_CHILD_TOOLS
    if not agent_def.can_delegate:
        agent_tools = agent_tools.filtered_copy(set(agent_tools.ids()) - blocked_child_tools)
    model = create_chat_model(api_key, model_cfg)
    tool_defs = agent_tools.tools_for_llm()
    tool_defs = filter_unavailable_lsp_tools(tool_defs, lsp_manager)
    tool_defs = strip_gemini_unsupported_schema_keys(tool_defs, resolve_protocol(config.model))

    if sub_messages is None:
        sub_messages = []

    messages = [HumanMessage(content=_task_payload(task_description, result_contract))]

    context_config = config.model_copy(deep=True)
    context_config.model = model_cfg
    interaction_mode = InteractionMode.PLAN if persona == PersonaName.PLAN else InteractionMode.AUTO
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
    contract_retry_count = 0
    has_successful_tool_work = False

    context, context_cache = RuntimeContextBuilder(
        config=context_config,
        workspace=config.workspace,
        base_system_prompt=build_base_system(context_config.user_profile.language),
        workflow_runtime=WORKFLOW_RUNTIME,
        persona_prompt=persona_prompt(),
        persona=persona,
        interaction_mode=interaction_mode,
        workflow_runs=workflow_context.runs,
        active_workflow_summaries=workflow_context.active,
        task_state=sub_task_state,
    ).build_incremental(context_cache)
    context.apply_to_messages(messages)

    snapshot_grants = None
    if permission_snapshot is not None:
        snapshot_grants = permission_snapshot.get_access_grants()

    ctx = ToolContext(
        workspace=config.workspace,
        session_id=session_id or "default",
        lsp_manager=lsp_manager,
        tool_registry=agent_tools,
        permission_mode=config.permission_mode.value,
        sandbox_readable_files=list(snapshot_grants.readable_files) if snapshot_grants is not None else list(config.sandbox_readable_files),
        sandbox_readable_dirs=list(snapshot_grants.readable_dirs) if snapshot_grants is not None else list(config.sandbox_readable_dirs),
        sandbox_writable_files=list(snapshot_grants.writable_files) if snapshot_grants is not None else list(config.sandbox_writable_files),
        sandbox_writable_dirs=list(snapshot_grants.writable_dirs) if snapshot_grants is not None else list(config.sandbox_writable_dirs),
        get_access_grants=(
            (lambda: permission_snapshot.get_access_grants())
            if permission_snapshot is not None
            else None
        ),
        get_revocation_epoch=(lambda: permission_snapshot.revocation_epoch) if permission_snapshot is not None else None,
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
            context_tokens = estimate_context_tokens_with_tools(
                llm_messages,
                tool_defs,
                config.model.model,
            )
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
            llm_failed_attempts = 0
            retry_status_active = False
            while True:
                try:
                    assistant_msg = await stream_llm(
                        model_with_tools,
                        llm_messages,
                        renderer,
                        resolve_protocol(config.model),
                    )
                    if retry_status_active and ui_port.via_events():
                        await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
                    break
                except Exception as e:
                    kind = _classify_llm_error(e)
                    if kind in {LLMErrorKind.NON_RETRYABLE, LLMErrorKind.CONTEXT_OVERFLOW}:
                        if retry_status_active and ui_port.via_events():
                            await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
                        raise
                    if llm_failed_attempts < _LLM_MAX_RETRIES:
                        llm_failed_attempts += 1
                        delay = _llm_retry_delay(llm_failed_attempts)
                        delay_str = str(int(delay)) if delay == int(delay) else str(delay)
                        retry_detail = f"retrying in {delay_str}s: {_clean_error_message(e)}"
                        if ui_port.via_events():
                            retry_status_active = True
                            await ui_port.events.emit(StatusUpdated(
                                status_id="llm:retry",
                                label="Retrying",
                                detail=retry_detail,
                            ))
                        else:
                            ui_port.ui.print(f"[dim]Retrying ({retry_detail})[/dim]")
                        await asyncio.sleep(delay)
                        continue
                    if retry_status_active and ui_port.via_events():
                        await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
                    raise
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
                if has_successful_tool_work and not _satisfies_result_contract(text, result_contract):
                    if contract_retry_count < _RESULT_CONTRACT_RETRY_LIMIT:
                        contract_retry_count += 1
                        step -= 1
                        guidance = _result_contract_retry_message(result_contract)
                        messages.append(HumanMessage(content=guidance))
                        continue
                    if tracker:
                        tracker.update(task_id, last_output=text[:200])
                        tracker.finish(task_id, "completed")
                    mark_finished("contract_unsatisfied")
                    return text
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
                        status="error",
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

            async def run_one(tc):
                tid = tc.get("name", "")
                targs = tc.get("args", {})
                cid = tc.get("id", "")
                if capture_tree and parent_node is not None:
                    capture.tool_call(tid, targs, tool_call_id=cid)
                result = await agent_tools.execute_tool(tid, targs, ctx)
                todo_state = todo_run_state_from_result(result) if tid == "todo" else None
                if todo_state_sink is not None and todo_state is not None and todo_state.total > 0:
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
                llm_content = maybe_persist_tool_result(
                    result.output,
                    cid,
                    tid,
                    session_id=ctx.session_id,
                    workspace=ctx.workspace,
                )
                return {
                    "tool_message": ToolMessage(
                        content=sanitize_tool_message_content(llm_content, workspace=ctx.workspace),
                        tool_call_id=cid,
                        status="success" if result_ok(result) else "error",
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
                    status="error",
                )
                for tc, reason in denied
            ]
            tool_msgs = [item["tool_message"] for item in executed]
            messages.extend(tool_msgs + denied_msgs)
            sub_messages.extend(tool_msgs + denied_msgs)

            for item in executed:
                metadata = getattr(item["result"], "metadata", {}) or {}
                if metadata.get("runtime_guard"):
                    continue
                if result_ok(item["result"]):
                    has_successful_tool_work = True
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
    parts = [task_description]
    if result_format:
        parts.append(
            "Result contract:\n"
            f"- schema_name: {schema_name}\n"
            f"- format: {result_format}\n"
            "Return the final answer using this contract."
        )
    return "\n\n".join(parts)


def _result_contract_fields(result_contract) -> list[str]:
    result_format = str(getattr(result_contract, "format", "") or "")
    fields: list[str] = []
    for raw_part in result_format.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", part)
        if match:
            fields.append(match.group(1))
    return fields


def _satisfies_result_contract(text: str, result_contract) -> bool:
    fields = _result_contract_fields(result_contract)
    if not fields:
        return True
    if not text.strip():
        return False

    matched = [
        field
        for field in fields
        if re.search(rf"(?im)^\s*(?:[-*]\s*)?{re.escape(field)}\s*[:=]", text)
    ]
    required = 1 if len(fields) == 1 else 2
    if fields[0] not in matched:
        return False
    return len(matched) >= required


def _result_contract_retry_message(result_contract) -> str:
    schema_name = str(getattr(result_contract, "schema_name", "") or "agent_result")
    result_format = str(getattr(result_contract, "format", "") or "").strip()
    return (
        "Your previous response did not satisfy the child-agent result contract. "
        "Do not return raw tool output or code snippets as the final answer.\n"
        "Summarize the completed delegated task using the required contract:\n"
        f"- schema_name: {schema_name}\n"
        f"- format: {result_format}"
    )
