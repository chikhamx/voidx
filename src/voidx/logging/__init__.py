"""voidx logging package — JSONL sinks for tool events, LLM exchanges, and internal errors."""

from voidx.logging.internal_error import log_internal_error
from voidx.logging.tool_log import log_tool_event

__all__ = ["log_internal_error", "log_tool_event"]

