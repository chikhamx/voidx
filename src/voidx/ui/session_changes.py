"""Turn-level file change tracker for the review bar."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from voidx.tools.base import resolve_safe


@dataclass
class FileChangeRecord:
    path: str
    added: int
    removed: int


@dataclass
class FileSnapshot:
    path: str
    resolved_path: Path
    existed: bool
    content: bytes


@dataclass
class RollbackResult:
    restored: list[str]
    removed: list[str]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class SessionChangeTracker:
    def __init__(self) -> None:
        self._files: dict[str, FileChangeRecord] = {}
        self._snapshots: dict[str, FileSnapshot] = {}
        self._workspace = "."
        self._visible = False

    def begin_turn(self, workspace: str) -> None:
        self._workspace = workspace
        self._files.clear()
        self._snapshots.clear()
        self._visible = False

    def finish_turn(self) -> None:
        self._visible = True

    def capture_tool_call(
        self,
        tool_name: str,
        args: dict,
        workspace: str,
        extra_paths: list[str] | None = None,
    ) -> None:
        if tool_name not in {"write", "edit", "lsp_format"}:
            return
        file_path = args.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return
        self.capture_file(file_path, workspace, extra_paths)

    def capture_file(
        self,
        file_path: str,
        workspace: str | None = None,
        extra_paths: list[str] | None = None,
    ) -> None:
        workspace = workspace or self._workspace
        resolved = resolve_safe(workspace, file_path, extra_paths)
        if resolved is None:
            return
        key = str(resolved)
        if key in self._snapshots:
            return
        existed = resolved.exists() and resolved.is_file()
        content = resolved.read_bytes() if existed else b""
        self._snapshots[key] = FileSnapshot(
            path=self._display_path(resolved, file_path, workspace),
            resolved_path=resolved,
            existed=existed,
            content=content,
        )

    def record_diff(self, diff_text: str) -> None:
        from voidx.ui.diff import parse_unified_diff

        parsed = parse_unified_diff(diff_text)
        for fd in parsed.files:
            key = fd.path
            if key in self._files:
                existing = self._files[key]
                existing.added += fd.added
                existing.removed += fd.removed
            else:
                self._files[key] = FileChangeRecord(
                    path=fd.path,
                    added=fd.added,
                    removed=fd.removed,
                )

    def rollback_current(self) -> RollbackResult:
        restored: list[str] = []
        removed: list[str] = []
        errors: list[str] = []

        for snapshot in self._snapshots.values():
            try:
                if snapshot.existed:
                    snapshot.resolved_path.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.resolved_path.write_bytes(snapshot.content)
                    restored.append(snapshot.path)
                elif snapshot.resolved_path.exists():
                    if snapshot.resolved_path.is_file():
                        snapshot.resolved_path.unlink()
                        removed.append(snapshot.path)
                    else:
                        errors.append(f"{snapshot.path}: path exists but is not a file")
            except Exception as exc:
                errors.append(f"{snapshot.path}: {exc}")

        if not errors:
            self.clear()
        return RollbackResult(restored=restored, removed=removed, errors=errors)

    @property
    def files(self) -> list[FileChangeRecord]:
        return list(self._files.values())

    @property
    def file_count(self) -> int:
        return len(self._files)

    @property
    def total_added(self) -> int:
        return sum(f.added for f in self._files.values())

    @property
    def total_removed(self) -> int:
        return sum(f.removed for f in self._files.values())

    @property
    def has_changes(self) -> bool:
        return self._visible and len(self._files) > 0

    def clear(self) -> None:
        self._files.clear()
        self._snapshots.clear()
        self._visible = False

    @staticmethod
    def _display_path(resolved: Path, original: str, workspace: str) -> str:
        try:
            return str(resolved.relative_to(Path(workspace).resolve()))
        except ValueError:
            return original


session_tracker = SessionChangeTracker()
