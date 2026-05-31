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

from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.llm.context import count_tokens, count_messages_tokens

PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
COMPACTION_BUFFER = 20_000
DEFAULT_TAIL_TURNS = 3
MIN_PRESERVE_RECENT = 2_000
MAX_PRESERVE_RECENT = 8_000
TOOL_OUTPUT_MAX_CHARS = 2_000
PRUNE_PROTECTED_TOOLS = {"agent"}

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
- Do not mention the summary process or that context was compacted."""


@dataclass
class Turn:
    """A conversation turn starts at a user message and ends before the next."""
    start: int   # index in messages list
    end: int     # index in messages list (exclusive)
    id: str      # user message id


class CompactionService:
    """Manages context window across the agent lifecycle."""

    def __init__(self, context_limit: int = 128_000, output_token_max: int = 8_192) -> None:
        self.context_limit = context_limit
        self.output_token_max = output_token_max
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

    def is_overflow(self, tokens: dict) -> bool:
        """Check if token usage exceeds usable window.
        tokens: {input, output, reasoning, cache_read, cache_write, total}
        """
        usable = self.usable_window()
        if usable <= 0:
            return False
        total = tokens.get("total", 0) or (
            tokens.get("input", 0) +
            tokens.get("output", 0) +
            tokens.get("reasoning", 0)
        )
        return total >= usable

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
        """
        turns_seen = 0
        accumulated = 0
        pruned_chars = 0
        to_prune: list[tuple[int, str]] = []  # (msg_index, truncated_text)

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]

            # Count turns by user messages
            if isinstance(msg, HumanMessage):
                turns_seen += 1
                if turns_seen > 2:
                    continue

            if isinstance(msg, AIMessage) and hasattr(msg, "summary") and msg.summary:
                break  # stop at compaction boundary

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
            return pruned_chars

        return 0

    # ── Layer 3: compaction ─────────────────────────────────────────────

    def _turns(self, messages: list) -> list[Turn]:
        """Split messages into turns. A turn starts at a user message
        and ends before the next user message."""
        result: list[Turn] = []
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                # Skip synthetic continuation messages
                content = str(getattr(msg, "content", ""))
                if "Continue if you have next steps" in content:
                    continue
                result.append(Turn(start=i, end=len(messages), id=getattr(msg, "id", str(i))))
        for j in range(len(result) - 1):
            result[j].end = result[j + 1].start
        return result

    def select(self, messages: list, tail_turns: int = DEFAULT_TAIL_TURNS) -> tuple[list, str | None]:
        """Split messages into head (to compact) and tail (to keep).
        Returns (head_messages, tail_start_id_or_None).

        Preserves recent turns up to preserve_recent_budget() tokens.
        """
        budget = self.preserve_recent_budget()
        turns = self._turns(messages)
        if not turns or tail_turns <= 0:
            return messages, None

        recent = turns[-tail_turns:]
        total = 0
        keep_start: int | None = None
        keep_id: str | None = None

        for turn in reversed(recent):
            turn_msgs = messages[turn.start:turn.end]
            size = count_messages_tokens(
                [{"role": "assistant" if isinstance(m, AIMessage) else "user",
                  "content": str(getattr(m, "content", ""))}
                 for m in turn_msgs]
            )
            if total + size <= budget:
                total += size
                keep_start = turn.start
                keep_id = turn.id
            else:
                break

        if keep_start is None or keep_start == 0:
            return messages, None

        return messages[:keep_start], keep_id

    # ── build compaction prompt ─────────────────────────────────────────

    def build_prompt(self, head_messages: list, previous_summary: str | None = None) -> str:
        """Build the compaction prompt. Extracts user message text from
        head, truncates tool outputs, strips media references."""
        context_parts: list[str] = []

        for msg in head_messages:
            content = str(getattr(msg, "content", ""))
            if not content.strip():
                continue

            if isinstance(msg, HumanMessage):
                context_parts.append(f"[User]: {content[:2000]}")
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

        return "\n\n".join([anchor, SUMMARY_TEMPLATE] + context_parts[:20])
