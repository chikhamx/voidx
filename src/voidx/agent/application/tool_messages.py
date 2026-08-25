"""Sanitize tool results before replaying them to the LLM."""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_TOOL_MESSAGE_MAX_CHARS = 4_000
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_SECRET_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]+)")


def sanitize_tool_message_content(
    content: object,
    *,
    workspace: str | None = None,
    max_chars: int = DEFAULT_TOOL_MESSAGE_MAX_CHARS,
) -> str:
    text = str(content)
    if workspace:
        try:
            workspace_path = str(Path(workspace).expanduser().resolve())
        except Exception:
            workspace_path = str(workspace)
        if workspace_path:
            text = text.replace(workspace_path, "<workspace>")
    try:
        home_path = str(Path.home().resolve())
    except Exception:
        home_path = ""
    if home_path:
        text = text.replace(home_path, "~")

    text = _KEY_VALUE_SECRET_RE.sub(r"\1\2[redacted]", text)
    text = _BEARER_SECRET_RE.sub(r"\1[redacted]", text)

    if max_chars > 0 and len(text) > max_chars:
        omitted = len(text) - max_chars
        text = text[:max_chars] + f"\n\n[Tool output truncated: omitted {omitted} chars]"
    return text


TOOL_OBSERVATION_MARKER = "voidx_tool_observation"
_NON_FALLBACK_OBSERVATION_TOOLS = frozenset({
    "turn",
    "goal",
    "goal_init",
    "goal_checkpoint",
    "goal_decision",
    "loop",
    "workflow",
    "todo",
    "compact",
    "clarify",
    "checkpoint",
})


def tool_observation_kwargs(
    *,
    source: str,
    tool_name: str = "",
    executed: bool,
    synthetic: bool,
    status: str,
) -> dict[str, object]:
    normalized_status = str(status or "error").strip().lower()
    fallback_eligible = bool(
        source == "tool_executor"
        and executed
        and not synthetic
        and normalized_status == "success"
        and tool_name not in _NON_FALLBACK_OBSERVATION_TOOLS
    )
    return {
        TOOL_OBSERVATION_MARKER: {
            "source": source,
            "executed": bool(executed),
            "synthetic": bool(synthetic),
            "status": normalized_status,
            "fallback_eligible": fallback_eligible,
        }
    }


def is_fallback_eligible_tool_observation(message: object) -> bool:
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict):
        return False
    observation = additional_kwargs.get(TOOL_OBSERVATION_MARKER)
    if not isinstance(observation, dict):
        return False
    return bool(
        observation.get("source") == "tool_executor"
        and observation.get("executed") is True
        and observation.get("synthetic") is False
        and observation.get("status") == "success"
        and observation.get("fallback_eligible") is True
    )
