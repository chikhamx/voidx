"""Git change stats for the input bar."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class ChangeStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    has_changes: bool = False


def get_change_stats(workspace: str = ".") -> ChangeStats:
    try:
        result = subprocess.run(
            ["git", "diff", "--shortstat", "--cached"],
            capture_output=True,
            text=True,
            timeout=3,
            cwd=workspace,
        )
        if result.returncode != 0:
            return ChangeStats()
        line = result.stdout.strip()
        if not line:
            return ChangeStats()
        return _parse_shortstat(line)
    except Exception:
        return ChangeStats()


def _parse_shortstat(line: str) -> ChangeStats:
    stats = ChangeStats()
    files_part = line.split(",")[0] if "," in line else line
    if "file" in files_part:
        num = "".join(c for c in files_part.split()[0] if c.isdigit())
        stats.files_changed = int(num) if num else 0
    for part in line.split(","):
        part = part.strip()
        if "insertion" in part:
            num = "".join(c for c in part.split()[0] if c.isdigit())
            stats.insertions = int(num) if num else 0
        elif "deletion" in part:
            num = "".join(c for c in part.split()[0] if c.isdigit())
            stats.deletions = int(num) if num else 0
    stats.has_changes = stats.files_changed > 0
    return stats
