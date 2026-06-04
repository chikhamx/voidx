"""Open changed files in the user's preferred code IDE."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from voidx.config import CodeIde, Settings


@dataclass(frozen=True)
class IdeCandidate:
    id: str
    label: str
    command: list[str] | None
    source: str
    available: bool


CLI_IDES: dict[str, tuple[str, list[str]]] = {
    CodeIde.TRAE.value: ("Trae", ["trae"]),
    CodeIde.CURSOR.value: ("Cursor", ["cursor"]),
    CodeIde.CODE.value: ("VS Code", ["code"]),
    CodeIde.WINDSURF.value: ("Windsurf", ["windsurf"]),
    CodeIde.ZED.value: ("Zed", ["zed"]),
    CodeIde.SUBLIME.value: ("Sublime Text", ["subl"]),
}

APP_IDES: dict[str, tuple[str, list[str]]] = {
    CodeIde.TRAE.value: ("Trae", ["Trae.app"]),
    CodeIde.CURSOR.value: ("Cursor", ["Cursor.app"]),
    CodeIde.CODE.value: ("Visual Studio Code", ["Visual Studio Code.app", "Visual Studio Code - Insiders.app"]),
    CodeIde.WINDSURF.value: ("Windsurf", ["Windsurf.app"]),
    CodeIde.ZED.value: ("Zed", ["Zed.app"]),
    CodeIde.SUBLIME.value: ("Sublime Text", ["Sublime Text.app"]),
    CodeIde.JETBRAINS.value: ("JetBrains Toolbox", [
        "IntelliJ IDEA.app",
        "PyCharm.app",
        "WebStorm.app",
        "PhpStorm.app",
        "GoLand.app",
        "RustRover.app",
        "CLion.app",
    ]),
    CodeIde.GHOSTTY.value: ("Ghostty", ["Ghostty.app"]),
}

TERMINAL_IDS = {CodeIde.GHOSTTY.value}


def normalize_ide(value: str | CodeIde | None) -> str:
    raw = str(value.value if isinstance(value, CodeIde) else value or "").strip().lower()
    return raw or CodeIde.TRAE.value


def detect_code_ides() -> list[IdeCandidate]:
    candidates: list[IdeCandidate] = []
    seen: set[tuple[str, str]] = set()

    for ide_id, (label, commands) in CLI_IDES.items():
        for command in commands:
            resolved = shutil.which(command)
            if resolved:
                _append_candidate(candidates, seen, IdeCandidate(
                    id=ide_id,
                    label=label,
                    command=[resolved],
                    source=f"cli:{command}",
                    available=True,
                ))
                break

    ghostty = shutil.which("ghostty")
    if ghostty:
        _append_candidate(candidates, seen, IdeCandidate(
            id=CodeIde.GHOSTTY.value,
            label="Ghostty",
            command=[ghostty],
            source="cli:ghostty",
            available=True,
        ))

    for ide_id, (label, app_names) in APP_IDES.items():
        for app_name in app_names:
            app_path = _find_app(app_name)
            if app_path is not None:
                _append_candidate(candidates, seen, IdeCandidate(
                    id=ide_id,
                    label=label,
                    command=["open", "-a", app_path.stem],
                    source=f"app:{app_path}",
                    available=True,
                ))
                break

    _append_candidate(candidates, seen, IdeCandidate(
        id=CodeIde.SYSTEM.value,
        label="System default",
        command=["open"],
        source="macOS open",
        available=shutil.which("open") is not None,
    ))
    return [candidate for candidate in candidates if candidate.available]


def preferred_ide(settings: Settings | None = None) -> str:
    if settings is None:
        return CodeIde.TRAE.value
    return normalize_ide(settings.get_code_ide())


def choose_ide(
    settings: Settings | None = None,
    detected: list[IdeCandidate] | None = None,
    preferred: str | CodeIde | None = None,
) -> IdeCandidate | None:
    detected = detected if detected is not None else detect_code_ides()
    if not detected:
        return None
    preferred_id = normalize_ide(preferred) if preferred is not None else preferred_ide(settings)
    if preferred_id != CodeIde.AUTO.value:
        for candidate in detected:
            if candidate.id == preferred_id:
                return candidate
    priority = [
        CodeIde.TRAE.value,
        CodeIde.CURSOR.value,
        CodeIde.CODE.value,
        CodeIde.WINDSURF.value,
        CodeIde.ZED.value,
        CodeIde.SUBLIME.value,
        CodeIde.JETBRAINS.value,
        CodeIde.GHOSTTY.value,
        CodeIde.SYSTEM.value,
    ]
    for ide_id in priority:
        for candidate in detected:
            if candidate.id == ide_id:
                return candidate
    return detected[0]


def code_ide_status(settings: Settings | None = None) -> str:
    configured = preferred_ide(settings)
    detected = detect_code_ides()
    selected = choose_ide(settings, detected)
    lines = [
        "[bold]Code IDE[/bold]",
        f"  Configured: [cyan]{configured}[/cyan]",
        f"  Selected: [cyan]{selected.label if selected else 'none'}[/cyan]",
        "",
        "  Detected:",
    ]
    if not detected:
        lines.append("    [dim]none[/dim]")
    else:
        for candidate in detected:
            marker = "✓" if selected and candidate.id == selected.id else " "
            lines.append(f"  {marker} [cyan]{candidate.id}[/cyan] — {candidate.label} [dim]({candidate.source})[/dim]")
    return "\n".join(lines)


def open_file_in_code_ide(
    file_path: str | Path,
    *,
    line: int = 1,
    settings: Settings | None = None,
    preferred: str | CodeIde | None = None,
) -> bool:
    path = Path(file_path).expanduser().resolve()
    candidate = choose_ide(settings, preferred=preferred)
    if candidate is None:
        return False
    command = build_open_command(candidate, path, line=line)
    if command is None:
        return False
    try:
        subprocess.Popen(command)
        return True
    except Exception:
        return False


def build_open_command(candidate: IdeCandidate, path: Path, *, line: int = 1) -> list[str] | None:
    if candidate.command is None:
        return None
    location = f"{path}:{line}"
    if _is_app_launcher(candidate.command):
        return _build_app_open_command(candidate, path, location, line=line)
    if candidate.id in {CodeIde.TRAE.value, CodeIde.CURSOR.value, CodeIde.CODE.value, CodeIde.WINDSURF.value}:
        return [*candidate.command, "--goto", location]
    if candidate.id == CodeIde.ZED.value:
        return [*candidate.command, str(path)]
    if candidate.id == CodeIde.SUBLIME.value:
        return [*candidate.command, location]
    if candidate.id == CodeIde.JETBRAINS.value:
        return [*candidate.command, str(path)]
    if candidate.id in TERMINAL_IDS:
        editor = _terminal_editor_command()
        if not editor:
            return [*candidate.command, str(path)]
        return [*candidate.command, "-e", *editor, f"+{line}", str(path)]
    if candidate.id == CodeIde.SYSTEM.value:
        return [*candidate.command, str(path)]
    return [*candidate.command, str(path)]


def _build_app_open_command(candidate: IdeCandidate, path: Path, location: str, *, line: int) -> list[str]:
    if candidate.id in {CodeIde.TRAE.value, CodeIde.CURSOR.value, CodeIde.CODE.value, CodeIde.WINDSURF.value}:
        return [*candidate.command, "--args", "--goto", location]
    if candidate.id in TERMINAL_IDS:
        editor = _terminal_editor_command()
        if editor:
            return [*candidate.command, "--args", "-e", *editor, f"+{line}", str(path)]
    return [*candidate.command, str(path)]


def _is_app_launcher(command: list[str]) -> bool:
    return len(command) >= 3 and Path(command[0]).name == "open" and command[1] == "-a"


def _append_candidate(candidates: list[IdeCandidate], seen: set[tuple[str, str]], candidate: IdeCandidate) -> None:
    key = (candidate.id, candidate.source)
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate)


def _find_app(app_name: str) -> Path | None:
    for base in (Path("/Applications"), Path.home() / "Applications"):
        path = base / app_name
        if path.exists():
            return path
    return None


def _terminal_editor_command() -> list[str]:
    for env_name in ("EDITOR", "VISUAL"):
        value = os.environ.get(env_name)
        if value:
            return shlex.split(value)
    for command in ("nvim", "vim", "nano"):
        resolved = shutil.which(command)
        if resolved:
            return [resolved]
    return []
