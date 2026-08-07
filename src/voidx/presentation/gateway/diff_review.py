"""Hunk-level diff review for the v2 gateway.

Provides DiffReviewSession which:
- Parses a unified diff into reviewable hunks
- Tracks approve/reject decisions per hunk
- Generates a filtered diff containing only approved hunks
- Exposes a JSON-serializable snapshot for the frontend
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from voidx.tooling.domain.diff import DiffHunk, DiffLine, FileDiff, StructuredDiff, parse_unified_diff


class HunkDecision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewableHunk(BaseModel):
    index: int
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[DiffLine] = Field(default_factory=list)
    decision: HunkDecision = HunkDecision.PENDING


class ReviewableFile(BaseModel):
    path: str
    old_path: str = ""
    new_path: str = ""
    operation: str = "Update"
    added: int = 0
    removed: int = 0
    hunks: list[ReviewableHunk] = Field(default_factory=list)


class DiffReviewSession:
    """Tracks hunk-level review decisions for a unified diff."""

    def __init__(self, files: list[ReviewableFile]) -> None:
        self.files = files

    @classmethod
    def from_diff(cls, diff_text: str) -> DiffReviewSession:
        structured = parse_unified_diff(diff_text)
        files: list[ReviewableFile] = []
        for fd in structured.files:
            hunks: list[ReviewableHunk] = []
            for i, h in enumerate(fd.hunks):
                hunks.append(ReviewableHunk(
                    index=i,
                    old_start=h.old_start,
                    old_count=h.old_count,
                    new_start=h.new_start,
                    new_count=h.new_count,
                    section=h.section,
                    lines=list(h.lines),
                ))
            files.append(ReviewableFile(
                path=fd.path or fd.new_path or fd.old_path,
                old_path=fd.old_path,
                new_path=fd.new_path,
                operation=fd.operation,
                added=fd.added,
                removed=fd.removed,
                hunks=hunks,
            ))
        return cls(files)

    def decide(self, file_path: str, hunk_index: int, decision: str) -> None:
        for f in self.files:
            if f.path == file_path:
                for h in f.hunks:
                    if h.index == hunk_index:
                        h.decision = HunkDecision(decision)
                return

    def summary(self) -> dict[str, int]:
        total = 0
        approved = 0
        rejected = 0
        pending = 0
        for f in self.files:
            for h in f.hunks:
                total += 1
                if h.decision == HunkDecision.APPROVED:
                    approved += 1
                elif h.decision == HunkDecision.REJECTED:
                    rejected += 1
                else:
                    pending += 1
        return {
            "total_hunks": total,
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
        }

    def approved_diff(self) -> str:
        """Return a unified diff containing only approved hunks."""
        lines: list[str] = []
        for f in self.files:
            approved_hunks = [h for h in f.hunks if h.decision == HunkDecision.APPROVED]
            if not approved_hunks:
                continue
            old_p = f.old_path or f.path
            new_p = f.new_path or f.path
            lines.append(f"--- {old_p}")
            lines.append(f"+++ {new_p}")
            for h in approved_hunks:
                lines.append(
                    f"@@ -{h.old_start},{h.old_count} "
                    f"+{h.new_start},{h.new_count} @@{h.section}"
                )
                for dl in h.lines:
                    prefix = " " if dl.kind == "context" else ("+" if dl.kind == "add" else "-")
                    lines.append(f"{prefix}{dl.text}")
        return "\n".join(lines) + "\n" if lines else ""

    def apply(self) -> list[str]:
        """Apply approved hunks to the working tree, returning changed file paths.

        For each file with approved hunks, reads the original content, rebuilds
        it by applying approved hunks (remove lines dropped, add lines inserted,
        context lines preserved), and writes the result back. Rejected or
        pending hunks are left as-is (their remove lines stay, add lines absent).
        """
        from pathlib import Path

        changed: list[str] = []
        for f in self.files:
            approved = [h for h in f.hunks if h.decision == HunkDecision.APPROVED]
            if not approved:
                continue
            target = f.path or f.new_path or f.old_path
            if not target:
                continue
            path = Path(target)
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            rebuilt = self._rebuild_file(original, approved)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rebuilt, encoding="utf-8")
            changed.append(target)
        return changed

    @staticmethod
    def _rebuild_file(original: str, hunks: list[ReviewableHunk]) -> str:
        """Rebuild file content by applying approved hunks to the original.

        Hunks are applied in old_start order. Lines outside any approved hunk
        are preserved unchanged.
        """
        lines = original.splitlines(keepends=False)
        result: list[str] = []
        cursor = 0  # 0-based index into `lines`, aligned to 1-based old_start
        for hunk in sorted(hunks, key=lambda h: h.old_start):
            # Advance to hunk start (1-based old_start → 0-based index)
            start_idx = max(0, hunk.old_start - 1)
            if start_idx > cursor:
                result.extend(lines[cursor:start_idx])
                cursor = start_idx
            for dl in hunk.lines:
                if dl.kind == "context":
                    if cursor < len(lines):
                        result.append(lines[cursor])
                    cursor += 1
                elif dl.kind == "add":
                    result.append(dl.text)
                elif dl.kind == "remove":
                    cursor += 1
        if cursor < len(lines):
            result.extend(lines[cursor:])
        return "\n".join(result) + ("\n" if original.endswith("\n") else "")

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "files": [
                {
                    "path": f.path,
                    "old_path": f.old_path,
                    "new_path": f.new_path,
                    "operation": f.operation,
                    "added": f.added,
                    "removed": f.removed,
                    "hunks": [
                        {
                            "index": h.index,
                            "old_start": h.old_start,
                            "old_count": h.old_count,
                            "new_start": h.new_start,
                            "new_count": h.new_count,
                            "section": h.section,
                            "lines": [dl.model_dump() for dl in h.lines],
                            "decision": h.decision.value,
                        }
                        for h in f.hunks
                    ],
                }
                for f in self.files
            ],
        }
