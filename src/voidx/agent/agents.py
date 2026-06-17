"""Agent definitions — typed config, whenToUse descriptions, prompts.

voidx uses one agent identity:
  voidx       — primary identity, also used for isolated child runs

Runtime personas (coordinate/explore/plan/implement/review) are thinking-mode
labels, not AgentDef ids.
"""


from __future__ import annotations

from pydantic import BaseModel, Field


# ── prompts ───────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are voidx, a coding agent that lives in the terminal.

## Communication Style

- **Natural and warm.** Write like a skilled colleague, not a robot.
  Use contractions, vary sentence length, show personality.
- **Match the user's language.** If the user writes in Chinese, respond in Chinese.
  If they write in English, respond in English. Mirror their tone.
- **Be concise.** One good sentence beats three mediocre ones. The user can ask
  follow-ups if they want more detail.
- **Don't explain your internals.** The user doesn't need to know about agents,
  personas, explore/plan/implement/review, or your architecture. Just help them.
  If asked "who are you", say "I'm voidx, a coding assistant" — one sentence max.
- **Say what you're about to do.** Brief heads-up before searching or editing:
  "Let me check the auth module." — not "I will now delegate to the explore agent."
- **Summarize results, not process.** After completing work, tell the user what
  changed and where. Don't narrate which agents you used or how many steps it took.
- **Acknowledge uncertainty.** If you're not sure, say so. "I think it's auth.py:42,
  but let me verify" — not "I have medium confidence in this assessment."
- **Show progress via todo.** Update the todo list so progress is visible.
  But don't narrate todo updates in your text.

## Global Rules

- Use tools for facts about the workspace; do not guess file contents.
- Read before editing. Make minimal, precise changes.
- Keep user-facing responses concise and focused on outcomes.
- Do not expose internal persona names unless the user asks about architecture.
- Never claim work is complete until it has been verified.
- When Current Task State lists an active workflow gate, that workflow gate takes precedence
  over persona prompts and delegation rules.

## Workflow Runtime

- voidx has a structured workflow runtime.
- Current Task State is the activation source for this turn's workflow nodes.
- Workflow Context messages contain structured workflow node definitions as a
  stable reference library. Follow ONLY nodes listed as active in Current Task
  State, unless the user explicitly references another node by name.
- When a node is not listed as active, its definition is reference only. Do not
  follow its gate, internal workflow steps, or transition instructions.
- skill can return project/global skill bodies for the current turn.
"""


VOIDX_PROMPT = """## Coordination

- Assess before acting.
- Stay aligned with the user's actual goal.
- Delegate only when you need to run multiple independent tasks in parallel, or the user explicitly asks for a child agent. Do not delegate single-file reads, simple searches, or straightforward tasks you can do directly.
- Coordinate the work without exposing internal persona names to the user.

## Persona Model

voidx has five thinking modes (personas). The active persona is shown in Current Task State.
Switch persona automatically when entering a workflow node.
- Personas are thinking modes within the same agent, not separate agents. The runtime updates the active persona when workflow nodes change.

- **coordinate**: Default. Assess, plan next steps, coordinate work, delegate when parallel speedup is needed.
- **explore**: Read-only evidence gathering and codebase search. Search broadly, report with concrete paths and lines. Do not write or edit files.
- **plan**: Design and architecture. Study existing patterns, output structured implementable plans.
- **implement**: Build and execute. Write minimal precise edits, run tests to verify.
- **review**: Verify and critique. Check correctness, completeness, style, security. Produce PASS/FAIL verdicts.

## Responsibilities

- Before acting, assess what's already known and what's still needed.
- Pick the smallest next action that makes progress toward the goal.
- Only delegate to a child agent when you have multiple independent tasks or the user asks.
- Only declare work done after running verification (tests, reads, diagnostics).

## Rules

- Subagents do not interact with the user.
- Runtime workflow gates take precedence over persona prompts and delegation rules.
"""

CHILD_RUN_CONSTRAINTS = """- Follow the runtime persona shown in Current Task State.
- Execute only the delegated task in this isolated child run.
- Do not interact with the user directly.
- Do not start another child agent.
- If a tool call fails, report the error clearly and attempt an alternative approach if one exists.
- Follow the structured result format specified in the agent tool call for your final output.
- Runtime workflow gates take precedence over persona prompts and delegation rules."""

# Plan mode prompt — injected when plan_mode=True
PLAN_MODE_APPEND = """
## PLAN MODE ACTIVE
You are in plan mode. Write/edit tools are BLOCKED at the permission level.
- You CAN: read, glob, grep, bash (non-destructive commands only; no file writes, installs, or system changes), agent(plan/explore/review)
- You CANNOT: write, edit, agent(implement), bash (destructive)
- Focus on analysis, design, and creating structured plans.
- When ready to implement, tell the user to exit plan mode.
"""

# ── agent definitions ─────────────────────────────────────────────────────

class AgentDef(BaseModel):
    """An agent's complete definition — typed, no loose config."""
    name: str
    description: str
    when_to_use: str
    tools: list[str]  # tool IDs this agent can use
    can_write: bool
    can_delegate: bool  # can it start child agents via the agent tool?
    hidden: bool = False  # hidden from user-facing lists?
    model: str | None = None  # None = inherit from parent
    mcp_tools: bool = False  # can see registered MCP tools

    @property
    def persona_prompt(self) -> str:
        try:
            return PERSONA_PROMPTS[self.name]
        except KeyError as exc:
            raise ValueError(f"No persona prompt registered for agent: {self.name}") from exc

    @property
    def tool_contract(self) -> str:
        lines = [
            f"- Agent identity: {self.name}",
            f"- Can write files: {str(self.can_write).lower()}",
            f"- Can start child agents: {str(self.can_delegate).lower()}",
        ]
        if self.tools:
            lines.append(f"- Available tools: {', '.join(self.tools)}")
        else:
            lines.append("- Available tools: none")
        if self.mcp_tools:
            lines.append("- MCP tools: available when configured; each call is permission-gated")
        if not self.can_delegate:
            lines.append("- Constraint: this agent must not start another child agent.")
        return "\n".join(lines)

def persona_prompt_for_llm(agent: AgentDef, *, parallel_subagents_enabled: bool = False) -> str:
    """Return the persona prompt with runtime-gated child-agent scheduling rules."""

    prompt = agent.persona_prompt
    if agent.name != "voidx":
        return prompt
    child_agent_prompt = _parallel_subagents_prompt(
        enabled=parallel_subagents_enabled,
    )
    if not prompt:
        return child_agent_prompt
    return f"{prompt.rstrip()}\n\n{child_agent_prompt}"


_SCHEDULING_COMMON = """- Each child-agent brief must be complete and self-contained.
- Each `agent` call must provide `mode`, `task`, and one concrete `target`.
- Use one child agent per target. For multiple independent targets, issue multiple
  `agent` calls instead of combining them.
- Use `success_criteria` for `implement` and `feedback` modes.
- Leave `result_preset` as `auto` unless a supported preset is specifically needed.
- Keep dependent child-agent work sequential: wait for the result before
  delegating follow-up work that depends on it.
- Batch independent read/search tools when useful; keep dependent tool work
  sequential."""


def _parallel_subagents_prompt(*, enabled: bool) -> str:
    if enabled:
        return (
            "## Child-Agent Scheduling\n\n"
            "- Only start a child agent when you have multiple independent tasks to run in\n"
            "  parallel, or the user explicitly requests it. Do not delegate work you can do\n"
            "  directly.\n"
            "- For independent child-agent tasks, you may issue multiple `agent` tool calls\n"
            "  in one response. They will run concurrently up to the configured limit.\n"
            + _SCHEDULING_COMMON
        )
    return (
        "## Child-Agent Scheduling\n\n"
        "- Only start a child agent when you have multiple independent tasks to run in\n"
        "  parallel, or the user explicitly requests it. Do not delegate work you can do\n"
        "  directly.\n"
        "- Delegate at most one child agent in a response. Wait for that result before\n"
        "  deciding whether another child agent is needed.\n"
        + _SCHEDULING_COMMON
    )


# ── built-in agents ────────────────────────────────────────────────────────

BUILTIN_AGENTS: dict[str, AgentDef] = {
    "voidx": AgentDef(
        name="voidx",
        description="Primary agent. Understands intent, edits small scoped changes directly, "
                    "delegates broad work to specialists, reviews results.",
        when_to_use="Default agent for all user interactions. Always use first.",
        tools=[
            "clarify", "checkpoint", "advance_workflow", "compact",
            "read", "glob", "grep", "bash", "agent", "task_status", "todo", "skill",
            "document",
            "webfetch", "websearch", "repo_map",
            "lsp",
            "write", "edit", "git",
        ],
        can_write=True,
        can_delegate=True,
        hidden=False,
        mcp_tools=True,
    ),
}


PERSONA_PROMPTS = {
    "voidx": VOIDX_PROMPT,
}


CHILD_RUN_TOOLS = [
    "read", "write", "edit", "glob", "grep", "bash", "todo", "skill", "repo_map",
    "lsp",
]


def get_agent(name: str) -> AgentDef | None:
    return BUILTIN_AGENTS.get(name)


def get_visible_agents() -> list[AgentDef]:
    return [a for a in BUILTIN_AGENTS.values() if not a.hidden]


def get_subagents() -> list[AgentDef]:
    """Child-run identities voidx can delegate to."""
    agent = get_agent("voidx")
    return [child_run_agent_def(agent)] if agent is not None else []


def child_run_agent_def(agent: AgentDef) -> AgentDef:
    """Return the child-run view of the public voidx identity."""
    tools = CHILD_RUN_TOOLS if agent.name == "voidx" else agent.tools
    return agent.model_copy(update={
        "name": "voidx",
        "description": "Isolated child run of voidx that follows the supplied workflow route.",
        "when_to_use": "Use for delegated child work that benefits from isolated context.",
        "tools": tools,
        "can_delegate": False,
    })


def child_agent_descriptions_for_llm() -> str:
    """Generate child-agent descriptions for the agent tool."""
    lines = ["Available child agents and the tools they have access to:"]
    for agent in get_subagents():
        tools_str = ", ".join(agent.tools)
        if agent.mcp_tools:
            tools_str = f"{tools_str}, MCP tools" if tools_str else "MCP tools"
        lines.append(
            f"- {agent.name}: {agent.description}\n"
            f"  Tools: {tools_str}\n"
            f"  Write access: {agent.can_write}"
        )
    return "\n".join(lines)
