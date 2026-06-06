"""Child agent execution loop."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.agents import BASE_SYSTEM_PROMPT, PLAN_MODE_APPEND, AgentDef
from voidx.agent.graph.convergence import (
    build_convergence_messages,
    generate_fallback_summary,
)
from voidx.agent.graph.runtime import console, ui
from voidx.agent.graph.streaming import extract_text, stream_llm
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.config import Config
from voidx.llm.provider import create_chat_model, resolve_protocol
from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.skills.registry import SkillRegistry
from voidx.skills.runtime import SkillRunState
from voidx.skills.service import SkillService
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
from voidx.tools.task_tracker import TaskTracker
from voidx.runtime.ui import CaptureConsole, OutputTree, StreamingRenderer


async def run_subagent(
    agent_def: AgentDef,
    task_description: str,
    model_override: str | None,
    api_key: str,
    config: Config,
    tracker: TaskTracker | None = None,
    capture_tree: OutputTree | None = None,
    parent_node=None,
    parent_messages: list | None = None,
    sub_messages: list | None = None,
    authorize_tools=None,
    debug: bool = True,
    agent_id: int = -1,
    session_id: str | None = None,
    usage_stats: UsageStats | None = None,
    lsp_manager=None,
    skill_selection=None,
) -> str:
    """Run a child agent. Child messages are appended to sub_messages
    (when provided) so the caller can place them after ToolMessages."""
    model_cfg = config.model.model_copy()
    if model_override:
        model_cfg.model = model_override
    elif agent_def.model:
        model_cfg.model = agent_def.model

    # Child agents use their own tool registry and cannot start nested agents.
    agent_tools = ToolRegistry()
    agent_tools.filter_tools(set(agent_def.tools) - {"agent", "task_status"})

    model = create_chat_model(api_key, model_cfg)
    tool_defs = [
        t for t in agent_tools.tools_for_llm()
        if t["function"]["name"] not in ("agent", "task_status")
    ]
    tool_defs = filter_unavailable_lsp_tools(tool_defs, lsp_manager)

    if sub_messages is None:
        sub_messages = []

    if parent_messages is not None:
        messages = []
        # Copy parent context: skip system prompts, agent-spawning AIMessages,
        # and their orphaned ToolMessages.
        skipped_ids: set[str] = set()
        for m in parent_messages:
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if name == "agent":
                        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                        if tc_id:
                            skipped_ids.add(tc_id)
        for m in parent_messages:
            if isinstance(m, SystemMessage):
                continue
            if isinstance(m, AIMessage) and m.tool_calls:
                if any(
                    (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) == "agent"
                    for tc in m.tool_calls
                ):
                    continue
            if isinstance(m, ToolMessage):
                tc_id = getattr(m, "tool_call_id", "")
                if tc_id in skipped_ids:
                    continue
            content = m.content
            if isinstance(content, list):
                text_parts = [
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                if isinstance(m, AIMessage):
                    messages.append(AIMessage(content="".join(text_parts), tool_calls=m.tool_calls))
                elif isinstance(m, HumanMessage):
                    messages.append(HumanMessage(content="".join(text_parts)))
                elif isinstance(m, ToolMessage):
                    messages.append(ToolMessage(content="".join(text_parts), tool_call_id=getattr(m, "tool_call_id", "")))
                else:
                    messages.append(type(m)(content="".join(text_parts)))
            else:
                messages.append(m)
        messages.append(HumanMessage(content=task_description))
    else:
        messages = [HumanMessage(content=task_description)]

    context_config = config.model_copy(deep=True)
    context_config.model = model_cfg
    interaction_mode = InteractionMode.PLAN.value if agent_def.name == "plan" else InteractionMode.AUTO.value
    mode_prompt = PLAN_MODE_APPEND if InteractionMode.parse(interaction_mode) == InteractionMode.PLAN else ""
    task_intent = _task_intent_for_agent(agent_def.name)
    skill_service = SkillService(
        SkillRegistry(config.workspace),
        selection=skill_selection,
    )
    skill_matches = skill_service.select(
        task_description,
        agent=agent_def.name,
        task_intent=task_intent,
        interaction_mode=interaction_mode,
    )
    RuntimeContextBuilder(
        config=context_config,
        workspace=config.workspace,
        base_system_prompt=BASE_SYSTEM_PROMPT,
        role_prompt=agent_def.role_prompt,
        mode_prompt=mode_prompt,
        tool_contract=agent_def.tool_contract,
        agent=agent_def.name,
        interaction_mode=interaction_mode,
        skill_instructions=[skill_service.render_instruction(match.skill) for match in skill_matches],
        skill_runs=[
            SkillRunState.from_match(
                match,
                phase="design" if interaction_mode == InteractionMode.PLAN.value else task_intent,
                scope=task_description,
            )
            for match in skill_matches
        ],
        active_skill_summaries=[f"{match.name} ({match.reason})" for match in skill_matches],
        current_user_text=task_description,
        task_intent=task_intent,
        session_date=datetime.now().astimezone().strftime("%Y-%m-%d %Z"),
        agent_id=agent_id,
    ).build().apply_to_messages(messages)

    ctx = ToolContext(
        workspace=config.workspace,
        lsp_manager=lsp_manager,
        sandbox_mode=config.sandbox_mode.value,
        sandbox_extra_paths=config.sandbox_workspace_write,
    )

    # Register with tracker
    task_id = f"sub_{agent_def.name}_{int(time.time())}"
    if tracker:
        tracker.start(task_id, agent_def.name, task_description, agent_def.max_steps)

    try:
        for step in range(1, agent_def.max_steps + 1):
            if tracker:
                tracker.update(task_id, step=step)

            if capture_tree and parent_node is not None:
                capture = CaptureConsole(capture_tree, parent_node, agent_id=agent_id)
                capture.step_header(step, agent_def.max_steps, agent_def.name)
            else:
                ui.step_header(step, agent_def.max_steps, agent_def.name)

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
            renderer = StreamingRenderer(console, debug=debug, agent_id=agent_id, headless=True)
            context_tokens = estimate_context_tokens(llm_messages, config.model.model)
            if usage_stats is not None:
                usage_stats.update_context(context_tokens)
            if session_id:
                await save_context_frame_from_messages(
                    session_id=session_id,
                    frame_kind="worker",
                    agent_role=agent_def.name,
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
                if capture_tree and parent_node is not None:
                    capture.tool_done(tid, 0.0, True, tool_call_id=cid)
                    capture.tool_result(result.output, tool_call_id=cid)
                return ToolMessage(
                    content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
                    tool_call_id=cid,
                )

            tool_msgs = await asyncio.gather(*[run_one(tc) for tc in approved])
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
    if agent_name == "implement":
        return "implement"
    if agent_name == "review":
        return "review"
    if agent_name == "plan":
        return "design"
    return "inspect"
