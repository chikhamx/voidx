"""Session lifecycle — startup screen and file change tracking."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rich.cells import cell_len
from rich.markup import escape

from voidx.paths import resolve_tool_path as _resolve_tool_path


# ---------------------------------------------------------------------------
# Startup screen
# ---------------------------------------------------------------------------


class StartupConsole(Protocol):
    width: int

    def print(self, *args, **kwargs) -> None: ...


def show_startup(
    console: StartupConsole,
    model: str,
    provider: str,
    workspace: str,
    session_title: str,
    is_new: bool,
) -> None:
    """Render the Claude Code style startup banner."""

    console.print("\n".join(render_startup_lines(
        console.width,
        model=model,
        provider=provider,
        workspace=workspace,
        session_title=session_title,
        is_new=is_new,
    )))


def render_startup_lines(
    console_width: int,
    *,
    model: str,
    provider: str,
    workspace: str,
    session_title: str,
    is_new: bool,
) -> list[str]:
    """Build Rich-markup startup banner lines."""

    from voidx import __version__
    import os

    folder_name = os.path.basename(workspace) or workspace

    greeting = "Welcome back!" if not is_new else "Welcome to voidx!"
    session_line = "New session" if is_new else f"Resumed: {session_title or 'previous session'}"
    info: list[tuple[str, str]] = [
        ("greeting", greeting),
        ("model", f"● Model  {provider}/{model}"),
        ("folder", f"▣ Workspace  {folder_name}"),
        ("session", f"↳ {session_line}"),
        ("hint", "Ask anything · / commands · /model switch · Ctrl+J newline"),
        ("hint", "PgUp/PgDn scroll transcript · @ attach · Ctrl+V clipboard"),
        ("hint", "Panels: ↑↓ select · Enter accept · Esc close"),
    ]

    title = f"voidx v{__version__}"
    logo = _cat_logo()
    logo_plain = [plain for plain, _ in logo]
    width = _banner_width(console_width, title, logo_plain, [line for _, line in info])
    logo_width = max(cell_len(line) for line in logo_plain)
    info_width = max(width - logo_width - 4, 16)

    rendered: list[str] = []
    title_gap = max(width - cell_len(title) - 1, 0)
    rendered.append(f"[dim]╭─ {escape(title)} {'─' * title_gap}╮[/dim]")
    for idx in range(max(len(logo), len(info))):
        left, styled_left = logo[idx] if idx < len(logo) else ("", "")
        item = info[idx] if idx < len(info) else ("", "")
        right = _fit_cell(item[1], info_width)
        plain = f"{left}{' ' * (logo_width - cell_len(left) + 4)}{right}"
        pad = max(width - cell_len(plain), 0)
        rendered.append(
            f"[dim]│[/dim] {styled_left}"
            f"{' ' * (logo_width - cell_len(left) + 4)}"
            f"{_style_info(item[0], right)}"
            f"{' ' * pad} [dim]│[/dim]"
        )
    rendered.append(f"[dim]╰{'─' * (width + 2)}╯[/dim]")

    return rendered


def _cat_logo() -> list[tuple[str, str]]:
    outline = "#8B6F62"
    eye = "#F2D6A2"
    bubble = "#C9B79A"
    return [
        (
            "       o     O        ",
            f"[{bubble}]       o     O        [/]",
        ),
        (
            "    /\\________/\\    ╭╮",
            f"[{outline}]    /\\________/\\    ╭╮[/]",
        ),
        (
            "   /  ◒      ◒  \\___││",
            f"[{outline}]   /  [/][{eye}]◒      ◒[/][{outline}]  \\___││[/]",
        ),
        (
            "  |   ▔      ▔      \\││",
            f"[{outline}]  |   [/][{eye}]▔      ▔[/][{outline}]      \\││[/]",
        ),
        (
            "  |                __╰╯",
            f"[{outline}]  |                __╰╯[/]",
        ),
        (
            "   \\______________/    ",
            f"[{outline}]   \\______________/    [/]",
        ),
    ]


def _banner_width(console_width: int, title: str, logo: list[str], info: list[str]) -> int:
    logo_width = max(cell_len(line) for line in logo)
    info_width = max(cell_len(line) for line in info)
    content_width = max(logo_width + 4 + info_width, cell_len(title) + 4)
    return min(content_width, max(console_width - 4, 44))


def _fit_cell(text: str, width: int) -> str:
    if cell_len(text) <= width:
        return text
    if width <= 3:
        return "." * max(width, 0)
    out = ""
    used = 0
    for char in text:
        char_width = cell_len(char)
        if used + char_width > width - 3:
            break
        out += char
        used += char_width
    return out + "..."


def _style_info(kind: str, text: str) -> str:
    escaped = escape(text)
    if kind == "greeting":
        return f"[bold white]{escaped}[/bold white]"
    if kind == "model":
        if text.startswith("● Model  "):
            return f"[#A3BE8C]●[/#A3BE8C] [dim]Model  [/dim][bold]{escape(text[9:])}[/bold]"
        return f"[bold]{escaped}[/bold]"
    if kind == "folder":
        prefix = "▣ Workspace  "
        if text.startswith(prefix):
            return f"[dim]▣ Workspace  [/dim][cyan]{escape(text[len(prefix):])}[/cyan]"
    if kind == "session":
        if text.startswith("↳ "):
            return f"[dim]↳ [/dim][cyan]{escape(text[2:])}[/cyan]"
    return f"[dim]{escaped}[/dim]"


# ---------------------------------------------------------------------------
# File change tracking
# ---------------------------------------------------------------------------


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
        if tool_name not in {"manage", "write", "replace"}:
            return
        if tool_name == "manage":
            paths = self._extract_manage_paths(args)
            for fp in paths:
                self.capture_file(fp, workspace, extra_paths)
            return
        file_path = args.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return
        self.capture_file(file_path, workspace, extra_paths)

    @staticmethod
    def _extract_manage_paths(args: dict) -> list[str]:
        op = args.get("op", "")
        if op == "move":
            paths = []
            for move in args.get("moves") or []:
                if isinstance(move, dict):
                    src = move.get("src")
                    dest = move.get("dest")
                    if isinstance(src, str) and src:
                        paths.append(src)
                    if isinstance(dest, str) and dest:
                        paths.append(dest)
            return paths
        raw = args.get("paths")
        if isinstance(raw, str) and raw:
            return [raw]
        if isinstance(raw, list):
            return [p for p in raw if isinstance(p, str) and p]
        return []

    def _capture_patch_files(
        self,
        patch: str,
        workspace: str,
        extra_paths: list[str] | None = None,
    ) -> None:
        from voidx.diffing import parse_unified_diff

        parsed = parse_unified_diff(patch)
        for file_diff in parsed.files:
            if file_diff.path and file_diff.path != "/dev/null":
                self.capture_file(file_diff.path, workspace, extra_paths)

    def capture_file(
        self,
        file_path: str,
        workspace: str | None = None,
        extra_paths: list[str] | None = None,
    ) -> None:
        workspace = workspace or self._workspace
        resolved = _resolve_tool_path(workspace, file_path, extra_paths)
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
        from voidx.ui.output.diff import parse_unified_diff

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

    @property
    def has_rollbackable_changes(self) -> bool:
        return self._visible and len(self._snapshots) > 0

    def change_summary_lines(self) -> list[str]:
        if not self._visible or not self._files:
            return []
        snap_by_path: dict[str, FileSnapshot] = {
            s.path: s for s in self._snapshots.values()
        }
        lines: list[str] = []
        for rec in self._files.values():
            snap = snap_by_path.get(rec.path)
            if snap is not None and not snap.existed:
                kind = "Created"
            else:
                kind = "Modified"
            lines.append(f"  [dim]{kind}[/dim]  [cyan]{escape(rec.path)}[/cyan]  [#A6E22E]+{rec.added}[/#A6E22E] [#FF4689]−{rec.removed}[/#FF4689]")
        return lines

    def rollback_summary_lines(self) -> list[str]:
        if not self._visible or not self._snapshots:
            return []
        lines = self.change_summary_lines()
        recorded_paths = {rec.path for rec in self._files.values()}
        for snapshot in self._snapshots.values():
            if snapshot.path in recorded_paths:
                continue
            kind = "Created" if not snapshot.existed else "Modified"
            lines.append(
                f"  [dim]{kind}[/dim]  [cyan]{escape(snapshot.path)}[/cyan]  [dim](snapshot only)[/dim]"
            )
        return lines

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
