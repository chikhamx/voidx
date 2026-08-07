"""Attachment limits shared across runtime and UI layers."""

from __future__ import annotations

import re


_ATTACHMENT_RE = re.compile(r'(?<!\S)(?:@(?:"([^"]+)"|(\S+))|\[image-([^\]]+)\])')
_PASTED_RE = re.compile(r"<pasted>\n.*?\n</pasted>", re.DOTALL)


def attachment_tokens(text: str) -> list[tuple[int, int, str]]:
    excluded = [(match.start(), match.end()) for match in _PASTED_RE.finditer(text)]
    tokens: list[tuple[int, int, str]] = []
    for match in _ATTACHMENT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in excluded):
            continue
        image_stem = match.group(3)
        raw_path = f":image:{image_stem}" if image_stem else match.group(1) or match.group(2)
        if raw_path:
            tokens.append((match.start(), match.end(), raw_path))
    return tokens

MAX_IMAGE_ATTACHMENT_BYTES = 5_000_000

__all__ = ["MAX_IMAGE_ATTACHMENT_BYTES", "attachment_tokens"]
