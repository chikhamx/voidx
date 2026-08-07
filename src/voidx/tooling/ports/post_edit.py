"""Narrow port for formatting a file range after an edit."""

from __future__ import annotations

from typing import Protocol


class PostEditFormatter(Protocol):
    enabled: bool

    async def format_range(self, file_path: str, range_: object) -> tuple[bool, str, str]: ...


__all__ = ["PostEditFormatter"]
