"""Authenticated opaque cursors for transcript window pagination."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

from voidx.presentation.protocol.v2.methods import MethodParamsError

_CURSOR_VERSION = 1


def encode_transcript_cursor(
    secret: bytes,
    *,
    thread_id: str,
    direction: str,
    boundary_turn_id: int,
    transcript_epoch: str,
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "thread_id": thread_id,
        "direction": direction,
        "boundary_turn_id": boundary_turn_id,
        "transcript_epoch": transcript_epoch,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64encode(hmac.new(secret, encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def decode_transcript_cursor(
    secret: bytes,
    cursor: object,
    *,
    thread_id: str,
    direction: str,
    transcript_epoch: str,
) -> int:
    """Authenticate a cursor and return its boundary, rejecting every mismatch."""
    try:
        if not isinstance(cursor, str) or not cursor:
            raise ValueError
        encoded, signature = cursor.split(".")
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise ValueError
        payload: Any = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or set(payload) != {
            "v", "thread_id", "direction", "boundary_turn_id", "transcript_epoch",
        }:
            raise ValueError
        boundary = payload["boundary_turn_id"]
        if (
            payload["v"] != _CURSOR_VERSION
            or payload["thread_id"] != thread_id
            or payload["direction"] != direction
            or payload["transcript_epoch"] != transcript_epoch
            or isinstance(boundary, bool)
            or not isinstance(boundary, int)
            or boundary < 0
        ):
            raise ValueError
        return boundary
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise MethodParamsError("invalid transcript cursor") from exc


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
