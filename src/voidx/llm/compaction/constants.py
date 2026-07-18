"""Compaction constants, prompt templates, and budget thresholds.

Three-layer context management (aligned with opencode):
  Layer 1 — prune:    Truncate old tool outputs. Zero API calls.
  Layer 2 — overflow:  Check if total_tokens >= usable_window.
  Layer 3 — compact:   LLM-generated structured summary. Preserve tail.
"""

from __future__ import annotations

PRUNE_ARGS_PLACEHOLDER_DIFF = "[omitted: see diff in tool result]"

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
