"""Validation for opaque session storage identifiers."""

from __future__ import annotations

import re


_SESSION_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def validate_session_storage_id(session_id: str) -> str:
    """Return an unchanged opaque id or reject unsafe/colliding path names."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session id must be a non-empty string")
    if session_id != session_id.lower() or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session id must use lowercase letters, digits, '_' or '-'")
    if session_id in {".", ".."} or session_id.rstrip(".") in _WINDOWS_RESERVED:
        raise ValueError("session id is reserved")
    return session_id


__all__ = ["validate_session_storage_id"]
