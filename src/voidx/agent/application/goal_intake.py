"""Goal intake — turn the first user message into a confirmed GoalSpec.

Runs one restricted LLM turn (read-only tools + clarify) that collects the
objective, acceptance condition, and achievement method. The model may ask the
user clarifying questions via the clarify tool; the final assistant message
must carry a JSON spec which is parsed and passed to the goal service.
"""
from __future__ import annotations

import json
import re

from voidx.agent.domain.goal import GOAL_PROFILE, GoalSpec, GoalToolView
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime.contracts import TurnRequest

_INTAKE_TOOL_IDS = frozenset(
    {
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "websearch",
        "webfetch",
        "mcp",
        "clarify",
    }
)

_INTAKE_PROMPT = """\
You are starting an autonomous Goal. Turn the user's request into a complete goal spec.

Required output (final message, valid JSON, nothing else):
{{"objective": "...", "acceptance_condition": "...", "achievement_method": "..."}}

Rules:
- objective: one sentence describing what must be accomplished.
- acceptance_condition: a concrete, verifiable condition that determines done.
- achievement_method: the approach the agent should take (optional; use "" if unknown).
- If any field cannot be determined, ask the user via the clarify tool before
  answering. Keep asking until the spec is complete and unambiguous.
- You may read project files (read/find/search/lsp/document) to ground the spec.
- Do not modify anything. Only the JSON above may be emitted as your final message.

User request:
{user_input}
"""

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class GoalIntakeError(RuntimeError):
    """Goal spec could not be determined from the intake turn."""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    block = _JSON_BLOCK_RE.search(text)
    candidate = block.group(1) if block else text
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


class GoalIntakeService:
    """Collect a confirmed GoalSpec from the first user message."""

    def __init__(self, runtime, goal_service) -> None:
        self._runtime = runtime
        self._goal_service = goal_service

    async def run(self, user_input: str, parent_thread_id: str | None, *, workspace: str = "") -> object:
        thread = AgentThread(
            thread_id=f"goal-intake:{parent_thread_id or 'host'}",
            session_id=parent_thread_id or "",
            workspace=workspace,
        )
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id,
            runtime_profile=GOAL_PROFILE,
            workspace=thread.workspace,
            tool_policy=GoalToolView.default(phase="intake").bind(_INTAKE_TOOL_IDS),
            goal_phase="intake",
        )
        result = await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=_INTAKE_PROMPT.format(user_input=user_input),
                context=context,
                runtime=None,
                persist_user_input=False,
            )
        )
        raw = _extract_json(getattr(result, "final_assistant_summary", "") or "")
        if raw is None:
            raise GoalIntakeError(
                "Could not determine a complete goal spec from the first message. "
                "Use /goal <objective> --accept <condition> to start explicitly."
            )
        try:
            spec = GoalSpec(
                objective=str(raw.get("objective") or "").strip(),
                acceptance_condition=str(raw.get("acceptance_condition") or "").strip(),
                achievement_method=str(raw.get("achievement_method") or "").strip(),
            )
        except ValueError as exc:
            raise GoalIntakeError(str(exc)) from exc
        if not spec.objective or not spec.acceptance_condition:
            raise GoalIntakeError(
                "Goal spec is incomplete. Use /goal <objective> --accept <condition> to start explicitly."
            )
        return await self._goal_service.start(parent_thread_id, spec)
