"""Context pressure decisions and stable convergence hints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage

from voidx.llm.compaction.service import CompactionService
from voidx.llm.message_markers import (
    CONTEXT_PRESSURE_MARKER,
    STEP_HINT_MARKER,
    is_context_pressure_message,
)

PressureLevel = Literal["none", "soft", "hard"]


@dataclass(frozen=True)
class ContextPressureDecision:
    over_soft: bool
    over_hard: bool
    can_compact: bool
    pressure_level: PressureLevel
    should_inject: bool
    turn_id: str
    turn_count: int
    pre_tokens: int
    soft_threshold: int
    hard_threshold: int
    reason: str


@dataclass(frozen=True)
class ContextPressureHintUpdate:
    state_messages: list[BaseMessage]
    message_delta: list[BaseMessage]
    pressure_id: str
    outcome: Literal["none", "hint_injected", "hint_present", "hint_upgraded"]


def evaluate_context_pressure(
    semantic_messages: list[BaseMessage],
    llm_context_tokens: int,
    *,
    compaction_service: CompactionService,
) -> ContextPressureDecision:
    turns = compaction_service._turns(semantic_messages)
    selection = compaction_service.select_preflight_details(semantic_messages)
    if not selection.should_compact:
        selection = compaction_service.select_details(semantic_messages)
    can_compact = selection.should_compact
    over_soft = compaction_service.is_soft_overflow({"total": llm_context_tokens})
    over_hard = compaction_service.is_overflow({"total": llm_context_tokens})
    level: PressureLevel = "hard" if over_hard else "soft" if over_soft else "none"
    reason = "hard_threshold" if over_hard else "soft_threshold" if over_soft else ""
    turn_id = turns[-1].id if turns else ""
    return ContextPressureDecision(
        over_soft=over_soft,
        over_hard=over_hard,
        can_compact=can_compact,
        pressure_level=level,
        should_inject=level != "none" and not can_compact,
        turn_id=turn_id,
        turn_count=len(turns),
        pre_tokens=llm_context_tokens,
        soft_threshold=compaction_service.soft_threshold(),
        hard_threshold=int(compaction_service.context_limit * 0.90),
        reason=reason,
    )


def upsert_context_pressure_hint(
    state_messages: list[BaseMessage],
    decision: ContextPressureDecision,
) -> ContextPressureHintUpdate:
    copied = list(state_messages)
    if not decision.should_inject or not decision.turn_id:
        return ContextPressureHintUpdate(copied, [], "", "none")

    pressure_id = f"voidx:context-pressure:{decision.turn_id}"
    existing_index = next(
        (
            index
            for index, message in enumerate(copied)
            if getattr(message, "id", None) == pressure_id
            and is_context_pressure_message(message)
        ),
        None,
    )
    existing_level = (
        str(copied[existing_index].additional_kwargs.get("pressure_level", ""))
        if existing_index is not None
        else ""
    )
    target_level = decision.pressure_level
    if existing_level == "hard" and target_level == "soft":
        return ContextPressureHintUpdate(copied, [], pressure_id, "hint_present")
    if existing_level == target_level:
        return ContextPressureHintUpdate(copied, [], pressure_id, "hint_present")

    hint = HumanMessage(
        id=pressure_id,
        content=render_pressure_hint(target_level),
        additional_kwargs={
            STEP_HINT_MARKER: True,
            CONTEXT_PRESSURE_MARKER: True,
            "pressure_level": target_level,
            "pressure_turn_id": decision.turn_id,
        },
    )
    if existing_index is None:
        copied.append(hint)
        outcome = "hint_injected"
    else:
        copied[existing_index] = hint
        outcome = "hint_upgraded"
    return ContextPressureHintUpdate(copied, [hint], pressure_id, outcome)


def render_pressure_hint(level: PressureLevel) -> str:
    if level == "hard":
        return (
            "Context pressure (hard): token usage is at or above the hard context threshold, "
            "and no older complete turn can be compacted yet. Converge immediately. "
            "Do not start non-essential tools; summarize current findings and finish this turn now."
        )
    return (
        "Context pressure (soft): the conversation is near the context budget, and there is no "
        "older complete turn available to compact yet. Stop expanding exploration, avoid broad "
        "searches or large reads, and finish this turn with the evidence already gathered."
    )


def current_context_pressure(
    state_messages: list[BaseMessage],
    turn_id: str,
) -> tuple[str, Literal["soft", "hard"]] | None:
    pressure_id = f"voidx:context-pressure:{turn_id}"
    for message in reversed(state_messages):
        if (
            getattr(message, "id", None) == pressure_id
            and is_context_pressure_message(message)
        ):
            level = str(message.additional_kwargs.get("pressure_level", "soft"))
            return pressure_id, "hard" if level == "hard" else "soft"
    return None


def hard_pressure_decision(decision: ContextPressureDecision) -> ContextPressureDecision:
    return ContextPressureDecision(
        over_soft=True,
        over_hard=True,
        can_compact=False,
        pressure_level="hard",
        should_inject=True,
        turn_id=decision.turn_id,
        turn_count=decision.turn_count,
        pre_tokens=decision.pre_tokens,
        soft_threshold=decision.soft_threshold,
        hard_threshold=decision.hard_threshold,
        reason="hard_threshold",
    )
