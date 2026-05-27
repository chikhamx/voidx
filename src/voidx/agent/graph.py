"""Agent graph — LangGraph state machine with 5-agent system.

Agents:
  orchestrator — primary, delegates, never writes code
  explore     — read-only codebase search
  plan        — read-only architecture design
  implement   — writes code, runs shell
  review      — read-only code review (PASS/FAIL/NEEDS_CHANGE)

Depth limit = 1: sub-agents cannot spawn further sub-agents.
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, StateGraph

from voidx.agent.agents import get_agent, AgentDef
from voidx.agent.prompts import SYSTEM_PROMPT
from voidx.agent.state import AgentState
from voidx.config import Config
from voidx.llm.compaction import CompactionService, SUMMARY_TEMPLATE
from voidx.llm.context import count_tokens, count_messages_tokens
from voidx.llm.instruction import InstructionService
from voidx.llm.provider import create_chat_model
from voidx.memory.session import (
    SessionInfo,
    MessageRow,
    create_session,
    load_messages,
    save_message,
    touch_session,
    update_title,
    _now,
)
from voidx.permission.service import PermissionService, PermissionRejectedError
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
from voidx.tools.task import TaskTool
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.todo import TodoWriteTool
from voidx.ui.console import VoidConsole, StreamingRenderer, _fmt_args
from voidx.ui.tui import live_input
from voidx.ui.tree import OutputTree, OutputNode
from voidx.ui.capture import CaptureConsole
from voidx.ui.browse import browse

ui = VoidConsole()
console = ui.console


# ── stream helper ──────────────────────────────────────────────────────────

async def _stream_llm(model, messages: list, renderer: StreamingRenderer) -> AIMessage:
    """Stream LLM response, render live, return merged AIMessage."""
    chunks: list[AIMessageChunk] = []

    try:
        async for chunk in model.astream(messages):
            chunks.append(chunk)
            content = chunk.content
            if isinstance(content, str) and content:
                renderer.feed_text(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        t = item.get("type", "")
                        if t in ("thinking", "redacted_thinking"):
                            text = item.get("thinking", "") or item.get("data", "")
                            if text:
                                renderer.feed_thinking(text)
                        elif t == "text":
                            renderer.feed_text(item.get("text", ""))
            meta = chunk.response_metadata
            if isinstance(meta, dict) and "thinking" in meta:
                renderer.feed_thinking(meta["thinking"])
    finally:
        renderer.done()


    if not chunks:
        return AIMessage(content="")

    merged = chunks[0]
    for c in chunks[1:]:
        merged = merged + c

    return AIMessage(
        content=merged.content,
        tool_calls=merged.tool_calls,
        response_metadata=merged.response_metadata,
        additional_kwargs=merged.additional_kwargs,
    )


# ── sub-agent runner ───────────────────────────────────────────────────────

async def _run_subagent(
    agent_def: AgentDef,
    task_description: str,
    model_override: str | None,
    api_key: str,
    config: Config,
    tracker: TaskTracker | None = None,
    capture_tree: OutputTree | None = None,
    parent_node = None,
    parent_messages: list | None = None,
    sub_messages: list | None = None,
) -> str:
    """Run a sub-agent. Sub-agent messages are appended to sub_messages
    (when provided) so the caller can place them after ToolMessages."""
    from voidx.llm.provider import create_chat_model

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
    tool_defs = [t for t in agent_tools.tools_for_llm() if t["function"]["name"] not in ("task", "task_status")]

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
                capture = CaptureConsole(capture_tree, parent_node)
                capture.step_header(step, agent_def.max_steps, agent_def.name)
            else:
                ui.step_header(step, agent_def.max_steps, agent_def.name)

            model_with_tools = model.bind_tools(tool_defs) if tool_defs else model
            renderer = StreamingRenderer(console)
            assistant_msg = await _stream_llm(model_with_tools, messages, renderer)
            messages.append(assistant_msg)
            sub_messages.append(assistant_msg)

            if not assistant_msg.tool_calls:
                text = _extract_text(assistant_msg)
                if tracker:
                    tracker.update(task_id, last_output=text[:200])
                    tracker.finish(task_id, "completed")
                return text

            # Update tracker with preview
            text_preview = _extract_text(assistant_msg)[:200]
            if tracker and text_preview:
                tracker.update(task_id, last_output=text_preview)

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

            tool_msgs = await asyncio.gather(*[run_one(tc) for tc in assistant_msg.tool_calls])
            messages.extend(tool_msgs)
            sub_messages.extend(tool_msgs)

        if tracker:
            tracker.finish(task_id, "completed")
        return _extract_text(messages[-1]) if messages else "Max steps reached."

    except Exception as e:
        if tracker:
            tracker.update(task_id, last_output=str(e)[:200])
            tracker.finish(task_id, "error")
        raise


def _extract_text(msg) -> str:
    content = msg.content if hasattr(msg, "content") else str(msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


# ── LangGraph nodes ────────────────────────────────────────────────────────

def _prepare(state: AgentState) -> dict:
    """Inject system prompt + agent context."""
    agent_name = state.get("agent", "orchestrator")
    agent_def = get_agent(agent_name)
    agent_prompt = agent_def.prompt if agent_def else SYSTEM_PROMPT

    workspace = state.get("workspace", ".")
    system = f"{agent_prompt}\n\nCurrent workspace: {workspace}"

    msgs = state.get("messages", [])
    if not any(isinstance(m, SystemMessage) for m in msgs):
        msgs.insert(0, SystemMessage(content=system))

    return {
        "step_count": state.get("step_count", 0) + 1,
        "max_steps": state.get("max_steps", agent_def.max_steps if agent_def else 50),
    }


class VoidXGraph:
    """The voidx agent as a LangGraph state machine."""

    def __init__(self, config: Config, api_key: str, session: SessionInfo | None = None):
        self.config = config
        self.api_key = api_key
        self.model = create_chat_model(api_key, config.model)
        self._session = session
        self._workspace = config.workspace

        # Build tool registry, wire task/todo/task_status to tracker
        self.tools = ToolRegistry()
        self._tracker = TaskTracker()
        task_tool = TaskTool(orchestrator_func=self._subagent_runner)
        self.tools.register("task", task_tool, task_tool.description, task_tool.parameters_schema())
        task_status_tool = TaskStatusTool(tracker=self._tracker)
        self.tools.register("task_status", task_status_tool, task_status_tool.description, task_status_tool.parameters_schema())
        # Replace built-in todo with tracker-aware version
        todo_tool = TodoWriteTool(tracker=self._tracker)
        self.tools.register("todo", todo_tool, todo_tool.description, todo_tool.parameters_schema())

        # AGENTS.md instruction service — refreshed each turn
        self._instruction = InstructionService(self._workspace)

        # Permission service — allow/deny/ask per tool call
        self._permission = PermissionService()

        # Plan mode — toggled by /plan and /unplan
        self._plan_mode: bool = False

        # File mtime staleness guard — shared across tool calls
        self._file_mtimes: dict[str, float] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list | None = None

        # Context compaction service — use model-specific limits
        model_name = config.model.model.lower()
        if "deepseek" in model_name:
            context_limit = 1_000_000  # deepseek models have ~1M context
        elif "claude" in model_name:
            context_limit = 200_000
        elif "gpt" in model_name or "o1" in model_name or "o3" in model_name:
            context_limit = 128_000
        else:
            context_limit = 128_000
        self._compaction = CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
        )

        self._build()

    async def _subagent_runner(self, agent_def: AgentDef, description: str, model_override: str | None) -> str:
        parent_messages = getattr(self, '_current_messages', None)
        self._sub_buffer = []
        if self._current_tree and self._turn_node:
            parent = self._turn_node
            return await _run_subagent(agent_def, description, model_override, self.api_key, self.config, self._tracker, self._current_tree, parent, parent_messages=parent_messages, sub_messages=self._sub_buffer)
        return await _run_subagent(agent_def, description, model_override, self.api_key, self.config, self._tracker, parent_messages=parent_messages, sub_messages=self._sub_buffer)

    def _build(self) -> None:
        workflow = StateGraph(AgentState)

        workflow.add_node("prepare", self._prepare_with_stream)
        workflow.add_node("call_llm", self._call_llm)
        workflow.add_node("execute_tools", self._execute_tools)
        workflow.add_node("finalize", self._finalize)

        workflow.set_entry_point("prepare")
        workflow.add_edge("prepare", "call_llm")
        workflow.add_conditional_edges("call_llm", self._router, {
            "execute": "execute_tools",
            "end": "finalize",
        })
        workflow.add_edge("execute_tools", "call_llm")
        workflow.add_edge("finalize", END)

        self.graph = workflow.compile()

    # ── nodes ───────────────────────────────────────────────────────────

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        base = _prepare(state)
        self._current_agent = get_agent(state.get("agent", "orchestrator"))

        # Inject AGENTS.md instructions into system prompt
        instructions = await self._instruction.system()
        if instructions:
            msgs = state.get("messages", [])
            if msgs and isinstance(msgs[0], SystemMessage):
                existing = msgs[0].content
                extra = "\n\n".join(instructions)
                msgs[0] = SystemMessage(content=f"{existing}\n\n{extra}")

        return base

    async def _call_llm(self, state: AgentState) -> dict:
        step = state.get("step_count", 0)
        max_s = state.get("max_steps", 50)
        if step > max_s:
            return {"should_continue": False}

        agent = get_agent(state.get("agent", "orchestrator"))
        agent_tool_ids = agent.tools if agent else None
        all_tool_defs = self.tools.tools_for_llm()

        # Filter tools based on agent's allowlist
        if agent_tool_ids is not None:
            tool_defs = [t for t in all_tool_defs if t["function"]["name"] in agent_tool_ids]
        else:
            tool_defs = all_tool_defs

        agent_name = state.get("agent", "orchestrator")
        ui.print()
        ui.step_header(step, max_s, agent_name)

        # ── LLM call with retry ────────────────────────────────────────
        max_retries = 2
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                renderer = StreamingRenderer(console)
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(model_with_tools, state["messages"], renderer)
                ui.print()
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = (attempt + 1) * 2
                    ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
                    await asyncio.sleep(delay)
                else:
                    ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
                    return {
                        "messages": [AIMessage(content=f"LLM call failed: {e}")],
                        "step_count": step,
                        "should_continue": False,
                    }
        else:
            # All retries exhausted
            return {
                "messages": [AIMessage(content=f"LLM call failed after all retries: {last_error}")],
                "step_count": step,
                "should_continue": False,
            }

        return {
            "messages": [assistant_msg],
            "step_count": step + 1,
        }

    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if state.get("step_count", 0) >= state.get("max_steps", 50):
                return "end"
            return "execute"
        return "end"

    async def _execute_tools(self, state: AgentState) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        self._current_messages = state["messages"]
        ctx = ToolContext(workspace=state.get("workspace", self._workspace), file_mtimes=self._file_mtimes)
        agent_name = state.get("agent", "orchestrator")
        session_id = self._session.id if self._session else "default"
        plan_mode = state.get("plan_mode", False)
        PLAN_DENIED_TOOLS = {"write", "edit", "bash"}

        tool_calls = last.tool_calls

        # ── Phase 1: repair names + batch permission pre-check ──────
        repaired: list[dict] = []
        for tc in tool_calls:
            tid = self._repair_tool_name(tc.get("name", ""))
            repaired.append({**tc, "name": tid})

        # Collect tools that need interactive approval
        need_ask: list[dict] = []
        approved: list[dict] = []
        denied: list[tuple[dict, str]] = []

        for tc in repaired:
            tid = tc["name"]
            targs = tc.get("args", {})
            pattern = self._build_pattern(tid, targs)

            # Plan mode block
            if plan_mode and tid in PLAN_DENIED_TOOLS:
                denied.append((tc, f"BLOCKED by plan mode: '{tid}' is not allowed."))
                continue
            if plan_mode and tid == "task" and targs.get("subagent_type") == "implement":
                denied.append((tc, "BLOCKED by plan mode: cannot delegate to implement."))
                continue

            # Quick check against defaults + session whitelist (no I/O)
            if tid in self._permission._session_allow:
                approved.append(tc)
            elif tid in self._permission._session_deny:
                denied.append((tc, f"Permission denied: {tid} is session-blocked."))
            elif tid in {"read", "glob", "grep", "webfetch", "websearch", "todo", "task_status", "repo_map", "task"}:
                approved.append(tc)
            elif tid == "bash":
                if self._is_safe_bash(targs.get("command", "")):
                    approved.append(tc)
                else:
                    need_ask.append(tc)
            elif tid in {"write", "edit"}:
                if agent_name == "implement":
                    approved.append(tc)
                else:
                    need_ask.append(tc)
            else:
                need_ask.append(tc)

        # Batch interactive ask for all tools that need it
        if need_ask:
            from voidx.ui.tui import live_choice

            tool_list = ", ".join(t["name"] for t in need_ask)
            ui.print("")
            ui.print(f"  [yellow]Allow tools: [bold]{tool_list}[/bold]?[/yellow]")
            choices = [
                ("Always", "a", "Allow all for this session"),
                ("Once",   "y", "Allow this once"),
                ("No",     "n", "Deny all"),
            ]
            try:
                choice = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: live_choice("", choices)
                )
            except (EOFError, KeyboardInterrupt):
                choice = None

            if choice is None:
                choice = "n"

            if choice == "a":
                for tc in need_ask:
                    self._permission._session_allow.add(tc["name"])
                ui.print(f"[dim]✓ {len(need_ask)} tools allowed for this session[/dim]")
                approved.extend(need_ask)
            elif choice == "y":
                approved.extend(need_ask)
            else:
                for tc in need_ask:
                    denied.append((tc, f"User denied: {tc['name']}"))

        # ── Phase 2: parallel execution of all approved tools ────────

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            cid = tc.get("id", "")

            # Real-time output
            ui.tool_call(tid, targs)

            # Tree recording (if tree exists)
            tc_node = None
            if self._current_tree and self._turn_node:
                gerund = ui._TOOL_GERUND.get(tid, tid + "ing")
                tc_node = self._current_tree.new_node(
                    parent=self._turn_node,
                    node_type="tool_call",
                    header=f"● {gerund}({_fmt_args(targs)})",
                    status="running",
                    collapsed=True,
                )

            t0 = time.monotonic()
            ok = True
            try:
                result = await self.tools.execute_tool(tid, targs, ctx)
            except Exception as e:
                from voidx.tools.base import ToolResult
                result = ToolResult(
                    output=f"Tool execution error: {e}",
                    metadata={"error": str(e)},
                )
                ok = False
            elapsed = time.monotonic() - t0
            ui.tool_done(tid, elapsed, ok)

            if tc_node:
                icon = "✓" if ok else "✗"
                tc_node.header += f"  {icon} ({elapsed:.1f}s)"
                tc_node.elapsed = elapsed
                tc_node.status = "done" if ok else "error"
                if getattr(result, "diff", None):
                    self._current_tree.new_node(
                        parent=tc_node, node_type="diff",
                        header="diff", body_lines=result.diff.split("\n")[:20],
                        collapsed=True,
                    )
                else:
                    lines = result.output[:600].split("\n")
                    self._current_tree.new_node(
                        parent=tc_node, node_type="tool_result",
                        header=lines[0][:100] if lines else "",
                        body_lines=lines, collapsed=True,
                    )

            return ToolMessage(content=result.output, tool_call_id=cid)

        # Run all approved tools in parallel
        executed = await asyncio.gather(*[execute_one(tc) for tc in approved])

        # Sub-agent messages are buffered in self._sub_buffer (never mutated
        # state["messages"] directly). Append them after ToolMessages so
        # tool_use→tool_result adjacency is preserved for ALL tool calls.
        extra: list = list(getattr(self, '_sub_buffer', []))
        self._sub_buffer = []

        # Denied tools get error messages
        denied_msgs = [
            ToolMessage(content=reason, tool_call_id=tc.get("id", ""))
            for tc, reason in denied
        ]

        return {"messages": list(executed) + extra + denied_msgs}

    @staticmethod
    def _repair_tool_name(tid: str) -> str:
        """Auto-repair common LLM tool name mistakes.
        Claude Code has experimental_repairToolCall for this."""
        tool_map = {
            # PascalCase → snake_case
            "Read": "read", "Write": "write", "Edit": "edit",
            "MultiEdit": "edit", "multiEdit": "edit", "multi_edit": "edit",
            "Glob": "glob", "Grep": "grep", "Bash": "bash",
            "Task": "task", "TodoWrite": "todo", "Todo": "todo",
            "WebFetch": "webfetch", "WebSearch": "websearch",
            # Legacy names
            "read_file": "read", "write_file": "write",
            "edit_file": "edit", "shell": "bash",
            # Misc
            "readfile": "read", "writefile": "write",
            "search": "grep", "find": "glob",
            "RepoMap": "repo_map", "repomap": "repo_map", "Repo_map": "repo_map",
        }
        return tool_map.get(tid, tool_map.get(tid.lower(), tid))

    @staticmethod
    def _build_pattern(tool: str, args: dict) -> str:
        """Build a permission pattern from tool args.
        For bash: use the command string.
        For file tools: use the file path.
        Default: "*"
        """
        if tool == "bash":
            return args.get("command", "*")
        if tool in ("read", "write", "edit"):
            return args.get("file_path", "*")
        if tool == "task":
            return args.get("subagent_type", "*")
        return "*"

    @staticmethod
    def _is_safe_bash(command: str) -> bool:
        """Check if a bash command is read-only (safe to auto-allow)."""
        import re
        stripped = command.strip()
        if not stripped or stripped.startswith("#"):
            return True
        # Redirection to file → write
        if re.search(r" > ", stripped) or re.search(r" >> ", stripped):
            return False
        if re.search(r"\|\s*tee\b", stripped):
            return False

        # Parse first program word (skip leading env VAR=val assignments)
        words = stripped.split()
        prog = words[0].lower()

        # ── git ──────────────────────────────────────────────────────
        if prog == "git" and len(words) > 1:
            sub = words[1]
            READ_ONLY_GIT = {
                "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
                "ls-files", "ls-tree", "describe", "shortlog", "reflog", "cherry",
                "whatchanged", "notes", "grep", "bisect",
                "config", "stash", "branch", "tag", "remote", "worktree",
            }
            if sub not in READ_ONLY_GIT:
                return False
            if sub == "stash":
                return len(words) > 2 and words[2] in ("list", "show")
            if sub == "bisect":
                return len(words) > 2 and words[2] in ("log", "view", "visualize")
            if sub in ("branch", "tag"):
                return "-d" not in words and "-D" not in words
            if sub == "remote":
                return "-v" in words or "--verbose" in words or len(words) == 2
            if sub == "worktree":
                return len(words) > 2 and words[2] == "list"
            return True

        # ── gh CLI ───────────────────────────────────────────────────
        if prog == "gh" and len(words) > 1:
            sub = words[1]
            if sub == "pr":
                return len(words) > 2 and words[2] in ("view", "list", "status", "checks", "diff")
            if sub == "issue":
                return len(words) > 2 and words[2] in ("view", "list", "status")
            if sub == "api":
                cmd_upper = stripped.upper()
                if "-X" in cmd_upper or "--method" in cmd_upper:
                    return "GET" in cmd_upper
                return True
            if sub in ("auth", "config", "completion", "secret"):
                return len(words) == 2 or (len(words) > 2 and words[2] in ("list", "status", "view"))
            return False

        # ── read-only shell commands ─────────────────────────────────
        READ_ONLY = {
            "ls", "dir", "cat", "head", "tail", "wc", "which", "where", "whereis",
            "echo", "printf", "pwd", "date", "whoami", "uname", "env", "printenv",
            "df", "du", "sort", "uniq", "cut", "tr", "column", "less", "more",
            "find", "grep", "egrep", "fgrep", "rg", "file", "stat", "od",
            "true", "false", "test", "[", "type", "basename", "dirname",
            "realpath", "readlink", "hostname", "id", "groups", "logname",
            "uptime", "free", "swapon", "lscpu", "lsblk", "lspci", "lsusb",
        }
        if prog in READ_ONLY:
            return True

        # ── package managers — read-only subcommands only ────────────
        if prog in ("pip", "pip3") and len(words) > 1:
            return words[1] in ("list", "show", "freeze", "config", "cache")
        if prog in ("npm", "npx") and len(words) > 1:
            return words[1] in ("list", "ls", "view", "info", "outdated")
        if prog == "cargo" and len(words) > 1:
            return words[1] in ("search", "doc", "readme")
        if prog == "go" and len(words) > 1:
            return words[1] in ("list", "doc", "version", "env")

        return False

    async def _finalize(self, state: AgentState) -> dict:
        return {}

    # ── public API ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Interactive REPL with orchestrator agent."""
        from voidx.ui.startup import show_startup
        import time as time_mod

        is_new = self._session is None
        if self._session is None:
            self._session = await create_session(workspace=self._workspace)

        self._any_messages_sent = False

        title = self._session.title
        if len(title) > 60:
            title = title[:57] + "..."

        show_startup(
            console=ui.console,
            model=self.config.model.model,
            provider=self.config.model.provider,
            workspace=self._workspace,
            session_title=title,
            is_new=is_new,
        )

        ui.print(f"[dim]  ? for shortcuts · ← for agents[/dim]")

        while True:
            try:
                from voidx.ui.commands import COMMANDS
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: live_input(COMMANDS)
                )
            except KeyboardInterrupt:
                ui.print("\n[dim]bye.[/dim]")
                break
            except EOFError:
                ui.print("\n[dim]bye.[/dim]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # ── slash command? ──────────────────────────────────────────
            if user_input.startswith("/"):
                if user_input.strip() in ("/exit", "/quit"):
                    break
                if user_input.strip() == "/":
                    # Bare / → show commands, don't send as message
                    from voidx.ui.commands import COMMANDS
                    ui.print("[bold]Commands:[/bold]")
                    for name, desc in COMMANDS:
                        ui.print(f"  [cyan]{name}[/cyan] — {desc}")
                    continue
                dispatched = await self._dispatch_slash(user_input)
                if dispatched:
                    continue
                # Not a known command → skip silently
                continue

            # ── regular message ─────────────────────────────────────────
            ui.print(f"❯ {user_input}")
            await self._run_once(user_input)

        # Clean up session if no messages were sent (empty new session)
        if self._session and not self._any_messages_sent:
            from voidx.memory.session import delete_session
            await delete_session(self._session.id)

    async def _run_once(self, user_text: str) -> None:
        from voidx.memory.session import load_messages

        t_turn_start = time.monotonic()
        self._current_tree = OutputTree()
        self._turn_node = self._current_tree.new_node(
            parent=self._current_tree.root,
            node_type="turn",
            header=f"● {user_text[:100]}",
            collapsed=False,
        )
        session_msgs = await load_messages(self._session.id) if self._session else []
        # Safety: if session is huge, only load recent messages
        if len(session_msgs) > 500:
            ui.warn(f"Session has {len(session_msgs)} messages — loading last 200")
            session_msgs = session_msgs[-200:]

        msgs = []
        for row in session_msgs:
            if row.role == "system":
                msgs.append(SystemMessage(content=row.content))
            elif row.role == "user":
                msgs.append(HumanMessage(content=row.content))
            elif row.role == "assistant":
                content = row.content
                # DeepSeek returns structured content (thinking blocks) as JSON arrays
                if isinstance(content, str) and content.startswith("[{") and self.config.model.provider == "deepseek":
                    try:
                        import json as _json
                        content = _json.loads(content)
                    except Exception:
                        pass
                msgs.append(AIMessage(content=content, tool_calls=row.tool_calls or []))
            elif row.role == "tool":
                msgs.append(ToolMessage(content=row.content, tool_call_id=row.tool_call_id or ""))

        msgs.append(HumanMessage(content=user_text))
        if self._session:
            await save_message(MessageRow(session_id=self._session.id, role="user", content=user_text, created_at=_now()))
            self._any_messages_sent = True

        initial: AgentState = {
            "messages": msgs,
            "workspace": self._workspace,
            "tool_results": {},
            "step_count": 0,
            "max_steps": 50,
            "should_continue": True,
            "agent": "orchestrator",
            "plan_mode": self._plan_mode,
        }

        # ── plan mode: inject warning into system prompt ──────────────
        if self._plan_mode:
            from voidx.agent.agents import PLAN_MODE_APPEND
            for i, msg in enumerate(msgs):
                if isinstance(msg, SystemMessage):
                    msgs[i] = SystemMessage(content=msg.content + "\n" + PLAN_MODE_APPEND)
                    break

        # ── compaction: check overflow before running ──────────────────
        head, tail_id = await self._maybe_compact(msgs, session_msgs)

        final = await self.graph.ainvoke(initial, {"recursion_limit": self.config.agent.recursion_limit})

        # ── prune old tool outputs after turn ──────────────────────────
        self._compaction.prune(list(final["messages"]))

        # Persist new messages
        if self._session:
            for msg in final["messages"]:
                # Only save messages newer than what we loaded
                if isinstance(msg, AIMessage):
                    # Preserve structured content (thinking blocks) for DeepSeek
                    raw_content = msg.content
                    if isinstance(raw_content, list):
                        import json as _json
                        saved = _json.dumps(raw_content, ensure_ascii=False)
                    else:
                        saved = str(raw_content)
                    await save_message(MessageRow(
                        session_id=self._session.id,
                        role="assistant",
                        content=saved,
                        tool_calls=msg.tool_calls if msg.tool_calls else None,
                        created_at=_now(),
                    ))
                elif isinstance(msg, ToolMessage):
                    await save_message(MessageRow(
                        session_id=self._session.id,
                        role="tool",
                        content=str(msg.content),
                        tool_call_id=getattr(msg, "tool_call_id", None),
                        created_at=_now(),
                    ))
            await touch_session(self._session.id)

            # Auto-title on first message
            if len(session_msgs) <= 1:
                title = user_text[:80] + ("..." if len(user_text) > 80 else "")
                await update_title(self._session.id, title)

        elapsed = time.monotonic() - t_turn_start
        ui.print(f"[dim]\n✻\n Churned for {elapsed:.0f}s[/dim]")
        ui.sep()

    # ── compaction ─────────────────────────────────────────────────────────

    async def _maybe_compact(self, messages: list, session_msgs: list) -> tuple[list | None, str | None]:
        """Check overflow and compact if needed."""
        total_tokens = count_messages_tokens(
            [{"role": "system" if isinstance(m, SystemMessage) else
              "user" if isinstance(m, HumanMessage) else
              "assistant" if isinstance(m, AIMessage) else "tool",
              "content": str(getattr(m, "content", ""))[:500]}
             for m in messages]
        )
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}

        if not self._compaction.is_overflow(tokens):
            return None, None

        ui.print("[yellow]Context overflow — compacting...[/yellow]")

        head_msgs, tail_id = self._compaction.select(messages)

        if not head_msgs or not tail_id:
            # Hard fallback: keep only last 6 messages
            keep = min(6, len(messages))
            ui.print(f"[dim]Aggressive truncation: keeping last {keep} messages[/dim]")
            before = len(messages)
            # Remove old messages, keep system + last N
            system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
            other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
            messages.clear()
            messages.extend(system_msgs)
            messages.extend(other_msgs[-keep:])
            return messages[:max(0, len(messages) - keep)], None

        # Run compaction agent
        try:
            summary = await self._run_compaction_agent(head_msgs, None)
        except Exception as e:
            ui.print(f"[dim]Compaction agent failed ({e}) — aggressive truncation[/dim]")
            keep = min(6, len(messages))
            system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
            other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
            messages.clear()
            messages.extend(system_msgs)
            messages.extend(other_msgs[-keep:])
            return messages[:max(0, len(messages) - keep)], None

        if summary:
            for i, msg in enumerate(messages):
                if isinstance(msg, SystemMessage):
                    messages[i] = SystemMessage(
                        content=f"{msg.content}\n\n## Conversation Summary\n{summary}"
                    )
                    break

            self._compaction.compaction_count += 1
            ui.print(f"[dim]Compacted: {len(head_msgs)} messages → summary[/dim]")

        return head_msgs, tail_id

    async def _run_compaction_agent(self, head_messages: list, previous_summary: str | None) -> str | None:
        """Run the compaction agent to generate a structured summary."""
        from voidx.agent.agents import COMPACTION_PROMPT

        prompt = self._compaction.build_prompt(head_messages, previous_summary)
        renderer = StreamingRenderer(console)

        messages = [SystemMessage(content=COMPACTION_PROMPT)]
        messages.append(HumanMessage(content=prompt))

        # Use a cheap/fast call for compaction — no tools
        assistant_msg = await _stream_llm(self.model, messages, renderer)
        text = _extract_text(assistant_msg)
        return text if text else None

    async def _dispatch_slash(self, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        from voidx.ui.commands import COMMANDS

        # Split into command + arguments
        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Check if it's a known command
        known = [n for n, _ in COMMANDS if n == cmd]
        if not known:
            return False  # not a command, treat as message

        ui.print()

        if cmd in ("/exit", "/quit"):
            return True  # caller checks "BREAK" via getattr

        if cmd == "/clear":
            await self._clear()
        elif cmd == "/list":
            await self._list_sessions()
        elif cmd.startswith("/resume"):
            await self._resume(inp)
        elif cmd.startswith("/title"):
            await self._set_title(inp)
        elif cmd == "/plan":
            self._plan_mode = True
            ui.print("[yellow]PLAN MODE active. /unplan to exit.[/yellow]")
        elif cmd == "/unplan":
            self._plan_mode = False
            ui.print("[dim]Plan mode exited.[/dim]")
        elif cmd.startswith("/allow"):
            tool = args or cmd.removeprefix("/allow").strip()
            if tool:
                self._permission.allow(tool)
        elif cmd.startswith("/deny"):
            tool = args or cmd.removeprefix("/deny").strip()
            if tool:
                self._permission.deny(tool)
        elif cmd == "/permissions":
            ui.print(self._permission.show_rules())
        elif cmd == "/compact":
            ui.print("[yellow]Compacting...[/yellow]")
        elif cmd == "/diff":
            await self._show_diff()
        elif cmd == "/help":
            ui.print("[bold]Commands:[/bold]")
            for name, desc in COMMANDS:
                ui.print(f"  [cyan]{name}[/cyan] — {desc}")
        return True

    async def _show_diff(self) -> None:
        """Show git working tree diff with syntax highlighting."""
        from voidx.ui.diff import git_diff, git_diff_stat
        stat = git_diff_stat(self._workspace)
        if stat:
            ui.print(f"[bold]Changes:[/bold]\n{stat}\n")
            diff_text = git_diff(self._workspace)
            if diff_text:
                ui.diff(diff_text)
            else:
                ui.print("[dim]No diff content.[/dim]")
        else:
            ui.print("[dim]No changes in working tree.[/dim]")

    async def _clear(self) -> None:
        if self._session:
            from voidx.memory.session import clear_messages, update_title, create_session
            await clear_messages(self._session.id)
            # Reset title so startup doesn't show stale text
            await update_title(self._session.id, "New session")
            # Reset tracker state
            self._tracker._todos = []
            self._permission.clear_session_permissions()
            self._plan_mode = False
        ui.print("[dim]✓ Session cleared — ready for a new conversation[/dim]")

    async def _list_sessions(self) -> None:
        from voidx.memory.session import list_sessions
        sessions = await list_sessions()
        if not sessions:
            ui.print("[dim]No saved sessions.[/dim]")
            return
        ui.print("[bold]Saved sessions:[/bold]")
        for s in sessions:
            marker = " *" if self._session and s.id == self._session.id else ""
            ui.print(f"  [cyan]{s.id}[/cyan]{marker} | {s.title[:60]} | {s.message_count} msgs")

    async def _resume(self, cmd: str) -> None:
        from voidx.memory.session import get_session
        sid = cmd.removeprefix("/resume").strip()
        if not sid:
            ui.error("Usage: /resume <session_id>")
            return
        session = await get_session(sid)
        if not session:
            ui.error(f"Session not found: {sid}")
            return
        self._session = session
        self._workspace = session.workspace
        ui.print(f"[dim]Resumed: {session.id} — {session.title} ({session.message_count} msgs)[/dim]")

    async def _set_title(self, cmd: str) -> None:
        if not self._session:
            return
        title = cmd.removeprefix("/title").strip()
        if title:
            await update_title(self._session.id, title)
            ui.print(f"[dim]Title set: {title}[/dim]")
