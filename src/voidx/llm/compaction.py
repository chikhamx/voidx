"""Context compaction — three-layer management aligned with opencode.

Layer 1 — prune:    Truncate old tool outputs. Zero API calls.
Layer 2 — overflow:  Check if total_tokens >= usable_window.
Layer 3 — compact:   LLM-generated structured summary. Preserve tail.

Token budget constants (from opencode):
  PRUNE_MINIMUM        = 20_000  — minimum to trigger prune
  PRUNE_PROTECT        = 40_000  — keep this many tokens of tool output
  COMPACTION_BUFFER    = 20_000  — reserved for output
  DEFAULT_TAIL_TURNS   = 3       — keep this many recent turns
  MIN_PRESERVE_RECENT  = 2_000
  MAX_PRESERVE_RECENT  = 8_000
  TOOL_OUTPUT_MAX_CHARS = 2_000
"""

from __future__ import annotations

PRUNE_ARGS_PLACEHOLDER_DIFF = "[omitted: see diff in tool result]"

import re
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.llm.context import count_tokens
from voidx.llm.message_markers import (
    STEP_HINT_MARKER,
    is_guidance_message,
    is_step_hint_message,
)
from voidx.llm.usage import estimate_context_tokens

PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
COMPACTION_BUFFER = 20_000
DEFAULT_TAIL_TURNS = 3
MIN_PRESERVE_RECENT = 2_000
MAX_PRESERVE_RECENT = 8_000
TOOL_OUTPUT_MAX_CHARS = 2_000
PRUNE_PROTECTED_TOOLS = {"agent"}
COMPACTION_MAX_RETRIES = 2
FALLBACK_SUMMARY_MAX_PER_MSG = 200
FALLBACK_SUMMARY_MAX_ITEMS = 8
COMPACTION_PROMPT_CONTEXT_MAX_CHARS = 60_000
COMPACTION_THRESHOLD = 0.90  # trigger when used >= 90% of context_limit

SUMMARY_TEMPLATE = """Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Deduplicate repeated requests, tool results, and progress updates.
- Prefer durable facts, decisions, constraints, open work, and final tool outcomes over step-by-step narration.
- When a previous summary exists, keep still-true details, drop stale details, and merge new facts without duplicating old bullets.
- Preserve failed tool calls and error messages — they are critical for debugging and avoiding repeated mistakes.
- Do not mention the summary process or that context was compacted."""

COMPACTION_REQUEST = """Summarize the conversation above into the structured format below.
Do not narrate step-by-step execution.
Preserve durable facts, explicit decisions, constraints, open work, and final tool outcomes.
Remove stale transient execution detail.
Keep final tool outcomes, changed files, verification results, and unresolved failures.
Write a structured summary only. Do not address the user, do not include markdown fences,
and do not invent facts that are not present in the conversation.

{previous_summary_section}

{template}"""




def _tool_result_has_diff(messages: list, ai_msg_index: int, tool_call_id: str) -> bool:
    """Check if the ToolMessage for a given tool_call_id contains a diff marker.

    Searches from ai_msg_index forward until the next HumanMessage (turn boundary).
    """
    for j in range(ai_msg_index + 1, len(messages)):
        msg = messages[j]
        if isinstance(msg, HumanMessage):
            break
        if hasattr(msg, "tool_call_id") and msg.tool_call_id == tool_call_id:
            content = str(getattr(msg, "content", ""))
            return "---" in content and "+++" in content
    return False


def _prune_ai_tool_call_args(
    tool_calls: list[dict],
    messages: list,
    ai_msg_index: int,
) -> tuple[list[dict] | None, int]:
    """Omit large content/new_string args in file-edit tool calls.

    Returns (new_tool_calls, saved_chars). new_tool_calls is None if no changes.
    Only prunes when the corresponding tool result contains a diff
    (so the LLM can still see the content via the diff).
    """
    changed = False
    saved_chars = 0
    new_tool_calls: list[dict] = []

    for tc in tool_calls:
        tc_copy = {**tc, "args": dict(tc.get("args", {}))}
        args = tc_copy["args"]
        name = tc.get("name", "")
        tc_id = tc.get("id", "")

        if name == "write" and "content" in args:
            placeholder = f"[omitted: {args['content'].count(chr(10)) + 1} lines written]"
            if len(args["content"]) > len(placeholder) and _tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["content"]) - len(placeholder)
                args["content"] = placeholder
                changed = True
        elif name == "replace" and "new_string" in args:
            placeholder = PRUNE_ARGS_PLACEHOLDER_DIFF
            if len(args["new_string"]) > len(placeholder) and _tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["new_string"]) - len(placeholder)
                args["new_string"] = placeholder
                changed = True
        elif name == "write" and args.get("op") in ("insert", "append") and "new_string" in args:
            placeholder = PRUNE_ARGS_PLACEHOLDER_DIFF
            if len(args["new_string"]) > len(placeholder) and _tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["new_string"]) - len(placeholder)
                args["new_string"] = placeholder
                changed = True

        new_tool_calls.append(tc_copy)

    return (new_tool_calls if changed else None, saved_chars)

@dataclass
class Turn:
    """A conversation turn starts at a user message and ends before the next."""
    start: int   # index in messages list
    end: int     # index in messages list (exclusive)
    id: str      # user message id


@dataclass(frozen=True)
class CompactionSelection:
    """Structured compaction split decision."""
    head: list
    tail_id: str | None
    keep_from: int
    mode: Literal["none", "normal", "full"]

    @property
    def should_compact(self) -> bool:
        return self.mode != "none" and bool(self.head) and self.tail_id is not None


class CompactionService:
    """Manages context window across the agent lifecycle."""

    def __init__(
        self,
        context_limit: int = 128_000,
        output_token_max: int = 8_192,
        *,
        soft_ratio: float = 0.75,
        post_target_ratio: float = 0.10,
    ) -> None:
        self.context_limit = context_limit
        self.output_token_max = output_token_max
        self.soft_ratio = soft_ratio
        self.post_target_ratio = post_target_ratio
        self.compaction_count: int = 0

    # ── token budget helpers ────────────────────────────────────────────

    def usable_window(self) -> int:
        """How many tokens we can safely use before needing compaction."""
        reserved = COMPACTION_BUFFER
        return max(0, self.context_limit - reserved - self.output_token_max)

    def preserve_recent_budget(self) -> int:
        """How many tokens worth of messages to preserve as 'tail'."""
        usable = self.usable_window()
        return min(MAX_PRESERVE_RECENT, max(MIN_PRESERVE_RECENT, int(usable * 0.25)))

    def soft_threshold(self) -> int:
        """Token level where preflight compaction should run."""
        if self.context_limit <= 0:
            return 0
        return int(min(self.context_limit * self.soft_ratio, self.usable_window()))

    def post_compaction_target(self) -> int:
        """Target token level after aggressive preflight compaction."""
        if self.context_limit <= 0:
            return 0
        return int(self.context_limit * self.post_target_ratio)

    def is_soft_overflow(self, tokens: dict) -> bool:
        total = _token_total(tokens)
        threshold = self.soft_threshold()
        return threshold > 0 and total >= threshold

    def is_overflow(self, tokens: dict) -> bool:
        """Check if token usage exceeds the compaction threshold.

        Triggers when used tokens >= COMPACTION_THRESHOLD (90%) of context_limit,
        i.e. when less than 10% of the context window remains.
        tokens: {input, output, reasoning, cache_read, cache_write, total}
        """
        if self.context_limit <= 0:
            return False
        total = _token_total(tokens)
        return total >= int(self.context_limit * COMPACTION_THRESHOLD)

    # ── Layer 1: prune tool outputs ─────────────────────────────────────

    def prune(self, messages: list) -> int:
        """Walk backwards through messages, truncating old tool outputs.
        Returns number of characters pruned.

        Rules:
        - Skip most recent 2 turns (user messages count as turn boundaries)
        - Protected tools (agent) are never pruned
        - Already compacted parts stop further pruning
        - Cumulative tool output > PRUNE_PROTECT → truncate to TOOL_OUTPUT_MAX_CHARS
        - Only prune if total pruned > PRUNE_MINIMUM
        - For previous-turn AIMessage tool_calls, omit large content/new_string args
          when the corresponding tool result contains a diff
        """
        turns_seen = 0
        accumulated = 0
        pruned_chars = 0
        to_prune: list[tuple[int, str]] = []  # (msg_index, truncated_text)
        ai_to_rebuild: dict[int, list[dict]] = {}  # (msg_index, new_tool_calls)

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]

            # Count turns by user messages
            if isinstance(msg, HumanMessage):
                turns_seen += 1

            if isinstance(msg, AIMessage) and hasattr(msg, "summary") and msg.summary:
                break  # stop at compaction boundary

            # Prune AIMessage tool_calls args for previous turns
            if (
                isinstance(msg, AIMessage)
                and turns_seen >= 1
                and hasattr(msg, "tool_calls")
                and msg.tool_calls
            ):
                new_tcs, saved = _prune_ai_tool_call_args(msg.tool_calls, messages, i)
                if new_tcs is not None:
                    ai_to_rebuild[i] = new_tcs
                    pruned_chars += saved
                continue

            # Tool messages have role="tool" and a tool_call_id
            if not hasattr(msg, "tool_call_id") or not msg.tool_call_id:
                continue

            tool_name = getattr(msg, "name", "")
            if tool_name in PRUNE_PROTECTED_TOOLS:
                continue

            content = str(getattr(msg, "content", ""))
            token_est = count_tokens(content)

            accumulated += token_est
            if accumulated <= PRUNE_PROTECT:
                continue

            # Protect most recent 2 turns from ToolMessage truncation
            if turns_seen < 2:
                continue

            if len(content) > TOOL_OUTPUT_MAX_CHARS:
                truncated = content[:TOOL_OUTPUT_MAX_CHARS] + (
                    f"\n\n[Tool output truncated for context: omitted {len(content) - TOOL_OUTPUT_MAX_CHARS} chars]"
                )
                pruned_chars += len(content) - len(truncated)
                to_prune.append((i, truncated))

        if pruned_chars > PRUNE_MINIMUM:
            for idx, truncated in to_prune:
                messages[idx] = type(messages[idx])(
                    content=truncated,
                    tool_call_id=messages[idx].tool_call_id,
                )

        for idx, new_tcs in ai_to_rebuild.items():
            messages[idx] = messages[idx].model_copy(update={"tool_calls": new_tcs})

        return pruned_chars

    # ── Layer 3: compaction ─────────────────────────────────────────────

    def _turns(self, messages: list) -> list[Turn]:
        """Split messages into turns. A turn starts at a user message
        and ends before the next user message."""
        result: list[Turn] = []
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                # Skip synthetic continuation messages
                if is_step_hint_message(msg) or is_guidance_message(msg):
                    continue
                content = str(getattr(msg, "content", ""))
                if "Continue if you have next steps" in content:
                    continue
                result.append(Turn(start=i, end=len(messages), id=getattr(msg, "id", None) or str(i)))
        for j in range(len(result) - 1):
            result[j].end = result[j + 1].start
        return result

    def select_details(self, messages: list, tail_turns: int = DEFAULT_TAIL_TURNS) -> CompactionSelection:
        """Split messages into head (to compact) and tail (to keep).

        Preserves recent turns up to preserve_recent_budget() tokens.
        Uses estimate_context_tokens for consistent counting with overflow checks.
        """
        budget = self.preserve_recent_budget()
        turns = self._turns(messages)
        if not turns or tail_turns <= 0:
            return CompactionSelection(messages, None, 0, "none")

        recent = turns[-tail_turns:]
        total = 0
        keep_start: int | None = None
        keep_id: str | None = None

        for turn in reversed(recent):
            turn_msgs = messages[turn.start:turn.end]
            size = estimate_context_tokens(turn_msgs)
            if total + size <= budget:
                total += size
                keep_start = turn.start
                keep_id = turn.id
            else:
                break

        minimum_tail_turn = _minimum_tail_turn(turns)
        if keep_start is None or keep_start > minimum_tail_turn.start:
            keep_turn = minimum_tail_turn
            if keep_turn.start == 0:
                return CompactionSelection([], None, 0, "none")
            return CompactionSelection(
                messages[:keep_turn.start],
                keep_turn.id,
                keep_turn.start,
                "full",
            )

        if keep_start == 0:
            return CompactionSelection(messages, None, 0, "none")

        return CompactionSelection(messages[:keep_start], keep_id, keep_start, "normal")

    def select_preflight_details(self, messages: list, *, model: str = "") -> CompactionSelection:
        """Select a deeply compacted head while preserving current and previous turns."""
        turns = self._turns(messages)
        if not turns:
            return CompactionSelection(messages, None, 0, "none")

        minimum_keep_index = max(0, len(turns) - 2)
        keep_start = turns[minimum_keep_index].start
        keep_id = turns[minimum_keep_index].id
        if keep_start == 0:
            return CompactionSelection(messages, None, 0, "none")

        target = self.post_compaction_target()
        for turn in reversed(turns[:minimum_keep_index]):
            candidate_start = turn.start
            candidate_tail = messages[candidate_start:]
            if estimate_context_tokens(candidate_tail, model) > target:
                break
            keep_start = candidate_start
            keep_id = turn.id

        if keep_start == 0:
            return CompactionSelection(messages, None, 0, "none")
        return CompactionSelection(messages[:keep_start], keep_id, keep_start, "normal")

    def select(self, messages: list, tail_turns: int = DEFAULT_TAIL_TURNS) -> tuple[list, str | None]:
        """Backward-compatible split API returning (head, tail_start_id)."""
        selection = self.select_details(messages, tail_turns=tail_turns)
        if not selection.should_compact:
            return messages, None
        return selection.head, selection.tail_id

    def truncate_head_to_budget(self, messages: list, *, budget: int, model: str) -> list:
        """Keep the newest complete turns from head messages within a token budget."""
        turns = self._turns(messages)
        if not turns or budget <= 0:
            return []

        kept: list = []
        total = 0
        for turn in reversed(turns):
            turn_msgs = messages[turn.start:turn.end]
            size = estimate_context_tokens(turn_msgs, model)
            if total + size > budget:
                break
            kept = [*turn_msgs, *kept]
            total += size
        return kept

    # ── build compaction prompt ─────────────────────────────────────────

    @staticmethod
    def fallback_summary(messages: list) -> str:
        """Generate a basic summary from messages when the compaction agent fails.
        Preserves user intent plus assistant decisions and tool outcomes."""
        user_parts: list[str] = []
        assistant_parts: list[str] = []
        tool_parts: list[str] = []
        file_parts: list[str] = []
        constraint_parts: list[str] = []
        next_step_parts: list[str] = []
        for msg in messages:
            if is_step_hint_message(msg):
                continue
            content = _message_text(msg).strip()
            if isinstance(msg, HumanMessage):
                if content:
                    prefix = "Guidance: " if is_guidance_message(msg) else ""
                    user_parts.append(prefix + _truncate_line(content, FALLBACK_SUMMARY_MAX_PER_MSG))
                    constraint_parts.extend(_extract_constraint_mentions(content))
                    next_step_parts.extend(_extract_next_step_mentions(content))
                    file_parts.extend(_extract_path_mentions(content))
            elif isinstance(msg, AIMessage):
                if content:
                    assistant_parts.append(_truncate_line(content, FALLBACK_SUMMARY_MAX_PER_MSG))
                    constraint_parts.extend(_extract_constraint_mentions(content))
                    next_step_parts.extend(_extract_next_step_mentions(content))
                    file_parts.extend(_extract_path_mentions(content))
                for tc in getattr(msg, "tool_calls", []) or []:
                    name = tc.get("name", "?")
                    args = _truncate_line(str(tc.get("args", {})), 160)
                    assistant_parts.append(f"Called tool {name} with {args}")
            elif isinstance(msg, ToolMessage) or getattr(msg, "tool_call_id", None):
                name = getattr(msg, "name", "") or getattr(msg, "tool_call_id", "") or "tool"
                if content:
                    tool_parts.append(f"{name}: {_truncate_line(content, FALLBACK_SUMMARY_MAX_PER_MSG)}")
                    file_parts.extend(_extract_path_mentions(content))

        user_parts = _dedupe(user_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
        assistant_parts = _dedupe(assistant_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
        tool_parts = _dedupe(tool_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
        constraint_parts = _dedupe(constraint_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
        next_step_parts = _dedupe(next_step_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
        file_parts = _dedupe(file_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]

        lines = [
            "## Goal",
            f"- {user_parts[-1] if user_parts else '[auto-extracted from compacted context]'}",
            "",
            "## Constraints & Preferences",
        ]
        lines.extend(_bullets(constraint_parts, empty="(none)"))
        lines.extend([
            "",
            "## Progress",
            "### Done",
        ])
        lines.extend(_bullets(assistant_parts, empty="(none)"))
        lines.extend([
            "",
            "### In Progress",
            "- (none)",
            "",
            "### Blocked",
            "- (none)",
            "",
            "## Key Decisions",
        ])
        lines.extend(_bullets([part for part in assistant_parts if part.startswith("Called tool ")], empty="(none)"))
        lines.extend([
            "",
            "## Next Steps",
        ])
        lines.extend(_bullets(next_step_parts, empty="(none)"))
        lines.extend([
            "",
            "## Critical Context",
        ])
        critical = [f"User requested: {part}" for part in user_parts] + [f"Tool result: {part}" for part in tool_parts]
        lines.extend(_bullets(critical, empty="(none)"))
        lines.extend([
            "",
            "## Relevant Files",
        ])
        lines.extend(_bullets(file_parts, empty="(none)"))
        return "\n".join(lines)

    def build_prompt(self, head_messages: list, previous_summary: str | None = None) -> str:
        """Build the compaction prompt. Extracts user message text from
        head, truncates tool outputs, strips media references."""
        context_parts: list[str] = []

        for msg in head_messages:
            if is_step_hint_message(msg):
                continue
            content = _message_text(msg)
            if not content.strip():
                continue

            if isinstance(msg, HumanMessage):
                label = "Guidance" if is_guidance_message(msg) else "User"
                context_parts.append(f"[{label}]: {content[:2000]}")
            elif isinstance(msg, AIMessage):
                # Extract text from assistant
                text = content[:2000]
                # Truncate tool calls in this message
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        tc_name = tc.get("name", "?")
                        tc_args = str(tc.get("args", {}))[:200]
                        text += f"\n  [called {tc_name}({tc_args})]"
                context_parts.append(f"[Assistant]: {text}")
            elif hasattr(msg, "tool_call_id"):
                # Tool message — truncate
                truncated = content[:TOOL_OUTPUT_MAX_CHARS]
                if len(content) > TOOL_OUTPUT_MAX_CHARS:
                    truncated += f"\n[truncated: {len(content)} chars total]"
                context_parts.append(f"[Tool result]: {truncated}")

        anchor = (
            "Update the anchored summary below using the conversation history above.\n"
            "Preserve still-true details, remove stale details, and merge in the new facts.\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>"
            if previous_summary
            else "Create a new anchored summary from the conversation history above."
        )

        history = _join_with_char_budget(context_parts, COMPACTION_PROMPT_CONTEXT_MAX_CHARS)
        return "\n\n".join([
            anchor,
            "## Conversation History\n" + (history if history else "(none)"),
            SUMMARY_TEMPLATE,
        ])


def _minimum_tail_turn(turns: list[Turn]) -> Turn:
    """Return the oldest turn that must remain live after full compaction.

    During the normal run loop compaction runs after the current user message
    is appended and before the assistant responds. In that case the final turn
    is only the current request, so the previous complete turn must also remain
    live.
    """
    last = turns[-1]
    if last.end == last.start + 1 and len(turns) >= 2:
        return turns[-2]
    return last


def _token_total(tokens: dict) -> int:
    return tokens.get("total", 0) or (
        tokens.get("input", 0) +
        tokens.get("output", 0) +
        tokens.get("reasoning", 0)
    )


def _message_text(msg: object) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return str(content)


def _truncate_line(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + f"... [truncated {len(compact) - limit} chars]"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _bullets(items: list[str], *, empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {item}" for item in items]



def _extract_constraint_mentions(text: str) -> list[str]:
    compact = _truncate_line(text, FALLBACK_SUMMARY_MAX_PER_MSG)
    lower = compact.lower()
    markers = (
        "keep ",
        "do not ",
        "don't ",
        "must ",
        "avoid ",
        "prefer ",
        "constraint",
        "requirement",
    )
    if not any(marker in lower for marker in markers):
        return []
    parts = _split_clause_mentions(compact)
    return [part for part in parts if any(marker in part.lower() for marker in markers)] or [compact]


def _extract_next_step_mentions(text: str) -> list[str]:
    compact = _truncate_line(text, FALLBACK_SUMMARY_MAX_PER_MSG)
    lower = compact.lower()
    markers = (
        r"\brun\b",
        r"\btests?\b",
        r"\bverify",
        r"\bstill need",
        r"\bnext\b",
        r"\btodo\b",
        r"\bfollow up",
    )
    if not any(re.search(marker, lower) for marker in markers):
        return []
    parts = _split_clause_mentions(compact)
    return [part for part in parts if any(re.search(marker, part.lower()) for marker in markers)] or [compact]


def _split_clause_mentions(text: str) -> list[str]:
    return [part.strip(" ,.;") for part in re.split(r"[;,.]\s*", text) if part.strip(" ,.;")]

def _extract_path_mentions(text: str) -> list[str]:
    paths: list[str] = []
    for raw in text.replace(",", " ").replace(")", " ").replace("(", " ").split():
        token = raw.strip("`'\"")
        if "/" not in token:
            continue
        if token.startswith(("/", "./", "../")) or "." in token.rsplit("/", 1)[-1]:
            paths.append(token.rstrip(":;"))
    return paths


def _join_with_char_budget(parts: list[str], budget: int) -> str:
    if budget <= 0:
        return ""
    kept: list[str] = []
    used = 0
    separator = "\n\n"
    for part in parts:
        extra = len(part) + (len(separator) if kept else 0)
        remaining = budget - used
        if remaining <= 0:
            break
        if extra <= remaining:
            kept.append(part)
            used += extra
            continue
        allowance = remaining - (len(separator) if kept else 0)
        if allowance > 80:
            kept.append(part[:allowance].rstrip() + "\n[conversation history truncated by char budget]")
        break
    return separator.join(kept)
