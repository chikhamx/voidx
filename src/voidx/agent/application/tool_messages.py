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
