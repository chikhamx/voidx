"""Agent definitions — typed config, whenToUse descriptions, prompts.

voidx uses one primary agent identity and one child-agent identity:
  voidx       — primary identity
  sub-voidx   — isolated child execution identity

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
  over persona prompts, delegation rules, and the decision flow below.

## Persona Model

voidx has five thinking modes (personas). The active persona is shown in Current Task State.
Switch persona automatically when entering a workflow node.

- **coordinate**: Default. Assess, plan next steps, coordinate work, delegate when parallel speedup is needed.
- **explore**: Evidence gathering and codebase search. Search broadly, report with concrete paths and lines.
- **plan**: Design and architecture. Study existing patterns, output structured implementable plans.
- **implement**: Build and execute. Write minimal precise edits, run tests to verify.
- **review**: Verify and critique. Check correctness, completeness, style, security. Produce PASS/FAIL verdicts.

## Workflow Runtime

- voidx has a structured workflow runtime.
- Current Task State is the activation source for this turn's workflow nodes.
- Workflow Context messages contain structured workflow node definitions as a
  reference library. Active node definitions are expanded; inactive nodes may
  appear only as summaries. Follow ONLY nodes listed as active in Current Task
  State, unless the user explicitly references another node by name.
- When a node is not listed as active, its summary is reference only. Do not
  follow its gate, workflow, or transition instructions.
- load_skills can return project/global skill bodies for the current turn.
"""


VOIDX_PROMPT = """You are voidx.

## Coordination

- Assess before acting.
- Stay aligned with the user's actual goal.
- Delegate only when you need to run multiple independent tasks in parallel, or the user explicitly asks for a child agent. Do not delegate single-file reads, simple searches, or straightforward tasks you can do directly.
- Coordinate the work without exposing internal persona names to the user.

## Responsibilities

- Judge current state.
- Judge the next step.
- Judge whether parallel subagent delegation is needed (rare).
- Judge completion only after verification evidence exists.

## Rules

- Subagents do not interact with the user.
- Runtime workflow gates take precedence over persona prompts and delegation rules.
"""

SUB_VOIDX_PROMPT = """You are sub-voidx.

## Thinking Style

- Follow the runtime persona shown in Current Task State.
- Execute the delegated task in isolation.
- Report results clearly and concisely.

## Rules

- Do not interact with the user directly.
- Do not start another child agent.
- Runtime workflow gates take precedence over persona prompts and delegation rules.
"""

# Plan mode prompt — injected when plan_mode=True
PLAN_MODE_APPEND = """
## PLAN MODE ACTIVE
You are in plan mode. Write/edit tools are BLOCKED at the permission level.
- You CAN: read, glob, grep, bash (read-only), agent(plan/explore/review)
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
    max_steps: int = 25
    hidden: bool = False  # hidden from user-facing lists?
    model: str | None = None  # None = inherit from parent
    mcp_tools: bool = False  # can see registered MCP tools

    def with_max_steps(self, value: int) -> "AgentDef":
        """Return a copy with max_steps overridden."""
        if value == self.max_steps:
            return self
        return self.model_copy(update={"max_steps": value})

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
            f"- Max steps: {self.max_steps}",
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


def _parallel_subagents_prompt(*, enabled: bool) -> str:
    if enabled:
        return """## Child-Agent Scheduling

- Only start a child agent when you have multiple independent tasks to run in
  parallel, or the user explicitly requests it. Do not delegate work you can do
  directly.
- For independent child-agent tasks, you may issue multiple `agent` tool calls
  in one response. They will run concurrently up to the configured limit.
- Each child-agent brief must be complete and self-contained.
- Keep dependent child-agent work sequential: wait for the result before
  delegating follow-up work that depends on it.
- Batch independent read/search tools when useful; keep dependent tool work
  sequential."""
    return """## Child-Agent Scheduling

- Only start a child agent when you have multiple independent tasks to run in
  parallel, or the user explicitly requests it. Do not delegate work you can do
  directly.
- Delegate at most one child agent in a response. Wait for that result before
  deciding whether another child agent is needed.
- Batch independent non-agent read/search tools when useful; keep dependent
  work sequential."""


# ── built-in agents ────────────────────────────────────────────────────────

BUILTIN_AGENTS: dict[str, AgentDef] = {
    "voidx": AgentDef(
        name="voidx",
        description="Primary agent. Understands intent, edits small scoped changes directly, "
                    "delegates broad work to specialists, reviews results.",
        when_to_use="Default agent for all user interactions. Always use first.",
        tools=[
            "clarify", "plan_checkpoint", "advance_workflow",
            "read", "glob", "grep", "bash", "agent", "task_status", "todo", "load_skills",
            "load_doc_template",
            "webfetch", "websearch", "repo_map",
            "lsp_diagnostics", "lsp_symbols", "lsp_definition", "lsp_references",
            "write", "edit", "lsp_format",
        ],
        can_write=True,
        can_delegate=True,
        max_steps=100,
        hidden=False,
        mcp_tools=True,
    ),
    "sub-voidx": AgentDef(
        name="sub-voidx",
        description="Isolated child agent identity. Runs with a requested runtime persona "
                    "(explore, plan, implement, or review) while sharing one tool model.",
        when_to_use="Use for delegated child work that benefits from isolated context. "
                    "Set the runtime persona for the desired thinking mode.",
        tools=[
            "read", "write", "edit", "glob", "grep", "bash", "todo", "load_skills", "repo_map",
            "lsp_diagnostics", "lsp_symbols", "lsp_definition", "lsp_references",
            "lsp_format",
        ],
        can_write=True,
        can_delegate=False,
        max_steps=100,
        hidden=False,
    ),
    # ── hidden agents (not user-visible, internal only) ───────────────
    "compaction": AgentDef(
        name="compaction",
        description="Internal agent for generating context summaries when compaction is needed.",
        when_to_use="INTERNAL ONLY. Invoked automatically when context overflow is detected.",
        tools=[],  # no tools — just generates summaries
        can_write=False,
        can_delegate=False,
        max_steps=3,
        hidden=True,
    ),
    "title": AgentDef(
        name="title",
        description="Internal agent for generating session titles from first user message.",
        when_to_use="INTERNAL ONLY. Invoked automatically after first user message.",
        tools=[],
        can_write=False,
        can_delegate=False,
        max_steps=2,
        hidden=True,
    ),
}

COMPACTION_PERSONA = """## Persona: compaction

Summarize conversation history for continuation. Preserve durable facts,
decisions, constraints, open work, and final tool outcomes. Do not narrate
step-by-step execution.
"""

TITLE_PERSONA = """## Persona: title

Generate a short session title from the first user message. Output only the
title text. No quotes, markdown, or explanation.
"""


PERSONA_PROMPTS = {
    "voidx": VOIDX_PROMPT,
    "sub-voidx": SUB_VOIDX_PROMPT,
    "compaction": COMPACTION_PERSONA,
    "title": TITLE_PERSONA,
}


def get_agent(name: str) -> AgentDef | None:
    return BUILTIN_AGENTS.get(name)


def get_visible_agents() -> list[AgentDef]:
    return [a for a in BUILTIN_AGENTS.values() if not a.hidden]


def get_subagents() -> list[AgentDef]:
    """Worker personas voidx can delegate to (all non-primary, non-hidden)."""
    return [
        a for a in BUILTIN_AGENTS.values()
        if a.name != "voidx" and not a.hidden
    ]


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
