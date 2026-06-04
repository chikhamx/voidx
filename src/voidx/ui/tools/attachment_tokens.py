"""Framework-agnostic attachment token helpers."""

from __future__ import annotations


def attachment_token_text(rel_path: str) -> str:
    if any(ch.isspace() for ch in rel_path):
        return f'@"{rel_path}"'
    return f"@{rel_path}"


def image_attachment_token_text(stem: str) -> str:
    return f"[image-{stem}]"
