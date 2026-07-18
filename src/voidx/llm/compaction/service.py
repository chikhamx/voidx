"""Layer 2/3 — overflow checks, turn splitting, and compaction selection.

:class:`CompactionService` manages the context window across the agent
lifecycle: token-budget thresholds, head/tail selection, and prompt building.
Layer 1 (prune) and the fallback summary live in sibling modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage

from voidx.llm.compaction.constants import (
    COMPACTION_BUFFER,
    COMPACTION_PROMPT_CONTEXT_MAX_CHARS,
    COMPACTION_THRESHOLD,
    DEFAULT_TAIL_TURNS,
    MAX_PRESERVE_RECENT,
    MIN_PRESERVE_RECENT,
    SUMMARY_TEMPLATE,
    TOOL_OUTPUT_MAX_CHARS,
)
from voidx.llm.compaction.fallback_summary import (
    fallback_summary as _fallback_summary,
    join_with_char_budget,
    message_text,
)
from voidx.llm.compaction.prune import prune_messages
from voidx.llm.message_markers import is_guidance_message, is_step_hint_message


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

        Delegates to :func:`prune_messages`; kept as a method for API
        compatibility with callers that hold a ``CompactionService`` instance.
        """
        return prune_messages(messages)

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
        from voidx.llm.compaction import estimate_context_tokens

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
        from voidx.llm.compaction import estimate_context_tokens

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
        from voidx.llm.compaction import estimate_context_tokens

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
        """Generate a basic summary when the compaction agent fails.

        Delegates to :func:`fallback_summary`; kept as a static method for
        API compatibility with existing callers.
        """
        return _fallback_summary(messages)

    def build_prompt(self, head_messages: list, previous_summary: str | None = None) -> str:
        """Build the compaction prompt. Extracts user message text from
        head, truncates tool outputs, strips media references."""
        context_parts: list[str] = []

        for msg in head_messages:
            if is_step_hint_message(msg):
                continue
            content = message_text(msg)
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

        history = join_with_char_budget(context_parts, COMPACTION_PROMPT_CONTEXT_MAX_CHARS)
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
