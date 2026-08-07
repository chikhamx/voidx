"""Mutable file tracking capability for a single Agent run."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from voidx.tooling.domain.diff import FileDiff


class FileStatePort(Protocol):
    def record_mtime(self, path: Path) -> None: ...
    def clear_read_coverage(self, path: Path) -> None: ...
    def check_staleness(self, path: Path) -> str | None: ...
    def check_read_coverage(self, path: Path, start_line: int, end_line: int, *, display_path: str | None = None) -> str | None: ...
    def remap_read_coverage_from_file_diff(self, path: Path, diff: FileDiff, *, old_ranges: list[dict]) -> None: ...


__all__ = ["FileStatePort"]
