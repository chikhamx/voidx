"""Agent definitions — typed config and whenToUse descriptions.

voidx uses one agent identity:
  voidx       — primary identity, also used for isolated child runs

Runtime personas (coordinate/explore/plan/implement/review) are thinking-mode
labels, not AgentDef ids.

Tool visibility is controlled by the ToolRegistry and permission layer,
not by a static whitelist on AgentDef.
"""


from __future__ import annotations

from pydantic import BaseModel

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
You are in plan mode. Write/edit/delete tools are BLOCKED at the permission level.
- You CAN: read, glob, grep, bash (non-destructive commands only; no file writes, installs, or system changes), agent(plan/explore/review)
- You CANNOT: write, edit, delete, agent(implement), bash (destructive)
- Focus on analysis, design, and creating structured plans.
- When ready to implement, tell the user to exit plan mode.
"""

# ── agent definitions ─────────────────────────────────────────────────────

class AgentDef(BaseModel):
    """An agent's complete definition — typed, no loose config."""
    name: str
    description: str
    when_to_use: str
    can_write: bool
    can_delegate: bool  # can it start child agents via the agent tool?
    hidden: bool = False  # hidden from user-facing lists?
    model: str | None = None  # None = inherit from parent

# ── built-in agents ────────────────────────────────────────────────────────

BUILTIN_AGENTS: dict[str, AgentDef] = {
    "voidx": AgentDef(
        name="voidx",
        description="Primary agent. Understands intent, edits small scoped changes directly, "
                    "delegates broad work to specialists, reviews results.",
        when_to_use="Default agent for all user interactions. Always use first.",
        can_write=True,
        can_delegate=True,
        hidden=False,
    ),
}


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
    return agent.model_copy(update={
        "name": "voidx",
        "description": "Isolated child run of voidx that follows the supplied workflow route.",
        "when_to_use": "Use for delegated child work that benefits from isolated context.",
        "can_delegate": False,
    })


def child_agent_descriptions_for_llm() -> str:
    """Generate child-agent descriptions for the agent tool."""
    lines = ["Available child agents:"]
    for agent in get_subagents():
        lines.append(
            f"- {agent.name}: {agent.description}\n"
            f"  Write access: {agent.can_write}"
        )
    return "\n".join(lines)
