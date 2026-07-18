"""Context compaction — three-layer management (prune / overflow / compact).

Public API is re-exported here so ``from voidx.llm.compaction import ...``
keeps working after the split into a package.  See the sibling modules for
implementation:

  - :mod:`constants`        — thresholds and prompt templates
  - :mod:`prune`            — Layer 1: truncate old tool outputs
  - :mod:`fallback_summary` — basic summary when the compaction agent fails
  - :mod:`service`          — Layer 2/3: overflow, selection, prompt building
"""

from __future__ import annotations

from voidx.llm.compaction.constants import (
    COMPACTION_BUFFER,
    COMPACTION_MAX_RETRIES,
    COMPACTION_PROMPT_CONTEXT_MAX_CHARS,
    COMPACTION_REQUEST,
    COMPACTION_THRESHOLD,
    DEFAULT_TAIL_TURNS,
    FALLBACK_SUMMARY_MAX_ITEMS,
    FALLBACK_SUMMARY_MAX_PER_MSG,
    MAX_PRESERVE_RECENT,
    MIN_PRESERVE_RECENT,
    PRUNE_ARGS_PLACEHOLDER_DIFF,
    PRUNE_MINIMUM,
    PRUNE_PROTECT,
    PRUNE_PROTECTED_TOOLS,
    SUMMARY_TEMPLATE,
    TOOL_OUTPUT_MAX_CHARS,
)
from voidx.llm.compaction.fallback_summary import (
    bullets,
    dedupe,
    extract_constraint_mentions,
    extract_next_step_mentions,
    extract_path_mentions,
    fallback_summary,
    join_with_char_budget,
    message_text,
    truncate_line,
)
from voidx.llm.compaction.prune import (
    prune_ai_tool_call_args,
    prune_messages,
    tool_result_has_diff,
)
from voidx.llm.compaction.service import (
    CompactionSelection,
    CompactionService,
    Turn,
)
from voidx.llm.message_markers import STEP_HINT_MARKER
from voidx.llm.usage import estimate_context_tokens

__all__ = [
    "COMPACTION_BUFFER",
    "COMPACTION_MAX_RETRIES",
    "COMPACTION_PROMPT_CONTEXT_MAX_CHARS",
    "COMPACTION_REQUEST",
    "COMPACTION_THRESHOLD",
    "CompactionSelection",
    "CompactionService",
    "DEFAULT_TAIL_TURNS",
    "FALLBACK_SUMMARY_MAX_ITEMS",
    "FALLBACK_SUMMARY_MAX_PER_MSG",
    "MAX_PRESERVE_RECENT",
    "MIN_PRESERVE_RECENT",
    "PRUNE_ARGS_PLACEHOLDER_DIFF",
    "PRUNE_MINIMUM",
    "PRUNE_PROTECT",
    "PRUNE_PROTECTED_TOOLS",
    "STEP_HINT_MARKER",
    "SUMMARY_TEMPLATE",
    "TOOL_OUTPUT_MAX_CHARS",
    "Turn",
    "bullets",
    "dedupe",
    "estimate_context_tokens",
    "extract_constraint_mentions",
    "extract_next_step_mentions",
    "extract_path_mentions",
    "fallback_summary",
    "join_with_char_budget",
    "message_text",
    "prune_ai_tool_call_args",
    "prune_messages",
    "tool_result_has_diff",
    "truncate_line",
]
