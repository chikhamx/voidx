"""Sub-agent execution loop."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.agents import AgentDef
from voidx.agent.graph_parts.runtime import console, ui
from voidx.agent.graph_parts.streaming import extract_text, stream_llm
from voidx.config import Config
from voidx.llm.provider import create_chat_model, resolve_protocol
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.capture import CaptureConsole
from voidx.ui.tree import OutputTree
from voidx.ui.console import StreamingRenderer


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
) -> str:
    """Run a sub-agent. Sub-agent messages are appended to sub_messages
    (when provided) so the caller can place them after ToolMessages."""
    model_cfg = config.model.model_copy()
    if model_override:
        model_cfg.model = model_override
    elif agent_def.model:
        model_cfg.model = agent_def.model

    # Sub-agents use their own tool registry (no task tool)
    agent_tools = ToolRegistry()
    all_tool_ids = agent_tools.ids()
    for tid in list(all_tool_ids):
        if tid not in agent_def.tools and tid != "task":
            agent_tools._tools.pop(tid, None)
            agent_tools._instances.pop(tid, None)
    agent_tools._tools.pop("task", None)
    agent_tools._instances.pop("task", None)
    agent_tools._tools.pop("task_status", None)
    agent_tools._instances.pop("task_status", None)

    model = create_chat_model(api_key, model_cfg)
    tool_defs = [
        t for t in agent_tools.tools_for_llm()
        if t["function"]["name"] not in ("task", "task_status")
    ]

    sub_prompt = agent_def.prompt + f"\n\nCurrent workspace: {config.workspace}"

    if sub_messages is None:
        sub_messages = []

    if parent_messages is not None:
        messages = [SystemMessage(content=sub_prompt)]
        # Copy parent context: skip system prompts, task-spawning AIMessages,
        # and their orphaned ToolMessages.
        skipped_ids: set[str] = set()
        for m in parent_messages:
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if name == "task":
                        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                        if tc_id:
                            skipped_ids.add(tc_id)
        for m in parent_messages:
            if isinstance(m, SystemMessage):
                continue
            if isinstance(m, AIMessage) and m.tool_calls:
                if any(
                    (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) == "task"
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
        messages = [
            SystemMessage(content=sub_prompt),
            HumanMessage(content=task_description),
        ]

    ctx = ToolContext(workspace=config.workspace)

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

            model_with_tools = model.bind_tools(tool_defs) if tool_defs else model
            renderer = StreamingRenderer(console, debug=debug, agent_id=agent_id)
            assistant_msg = await stream_llm(
                model_with_tools,
                messages,
                renderer,
                resolve_protocol(config.model),
            )
            messages.append(assistant_msg)
            sub_messages.append(assistant_msg)

            if not assistant_msg.tool_calls:
                text = extract_text(assistant_msg)
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
                    capture.tool_call(tid, targs)
                result = await agent_tools.execute_tool(tid, targs, ctx)
                if capture_tree and parent_node is not None:
                    capture.tool_done(tid, 0.0, True)
                    capture.tool_result(result.output)
                return ToolMessage(content=result.output, tool_call_id=cid)

            tool_msgs = await asyncio.gather(*[run_one(tc) for tc in approved])
            denied_msgs = [
                ToolMessage(content=reason, tool_call_id=tc.get("id", ""))
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
