"""Open changed files in the user's preferred code IDE."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from typing import Any

from enum import Enum


class CodeIde(str, Enum):
    """Preferred app for opening files from the review panel."""

    AUTO = "auto"
    TRAE = "trae"
    CURSOR = "cursor"
    CODE = "code"
    WINDSURF = "windsurf"
    ZED = "zed"
    SUBLIME = "sublime"
    JETBRAINS = "jetbrains"
    GHOSTTY = "ghostty"
    SYSTEM = "system"



@dataclass(frozen=True)
class IdeCandidate:
    id: str
    label: str
    command: list[str] | None
    source: str
    available: bool


CLI_IDES: dict[str, tuple[str, list[str]]] = {
    'trae': ("Trae", ["trae"]),
    'cursor': ("Cursor", ["cursor"]),
    'code': ("VS Code", ["code"]),
    'windsurf': ("Windsurf", ["windsurf"]),
    'zed': ("Zed", ["zed"]),
    'sublime': ("Sublime Text", ["subl"]),
}

APP_IDES: dict[str, tuple[str, list[str]]] = {
    'trae': ("Trae", ["Trae.app"]),
    'cursor': ("Cursor", ["Cursor.app"]),
    'code': ("Visual Studio Code", ["Visual Studio Code.app", "Visual Studio Code - Insiders.app"]),
    'windsurf': ("Windsurf", ["Windsurf.app"]),
    'zed': ("Zed", ["Zed.app"]),
    'sublime': ("Sublime Text", ["Sublime Text.app"]),
    'jetbrains': ("JetBrains Toolbox", [
        "IntelliJ IDEA.app",
        "PyCharm.app",
        "WebStorm.app",
        "PhpStorm.app",
        "GoLand.app",
        "RustRover.app",
        "CLion.app",
    ]),
    'ghostty': ("Ghostty", ["Ghostty.app"]),
}

TERMINAL_IDS = {'ghostty'}


def normalize_ide(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    return raw or 'trae'


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
            id='ghostty',
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
        id='system',
        label="System default",
        command=["open"],
        source="macOS open",
        available=shutil.which("open") is not None,
    ))
    return [candidate for candidate in candidates if candidate.available]


def preferred_ide(settings: Any | None = None) -> str:
    if settings is None:
        return 'trae'
    return normalize_ide(settings.get_code_ide())


def choose_ide(
    settings: Any | None = None,
    detected: list[IdeCandidate] | None = None,
    preferred: str | CodeIde | None = None,
) -> IdeCandidate | None:
    detected = detected if detected is not None else detect_code_ides()
    if not detected:
        return None
    preferred_id = normalize_ide(preferred) if preferred is not None else preferred_ide(settings)
    if preferred_id != 'auto':
        for candidate in detected:
            if candidate.id == preferred_id:
                return candidate
    priority = [
        'trae',
        'cursor',
        'code',
        'windsurf',
        'zed',
        'sublime',
        'jetbrains',
        'ghostty',
        'system',
    ]
    for ide_id in priority:
        for candidate in detected:
            if candidate.id == ide_id:
                return candidate
    return detected[0]


def code_ide_status(settings: Any | None = None) -> str:
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
    settings: Any | None = None,
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
    if candidate.id in {'trae', 'cursor', 'code', 'windsurf'}:
        return [*candidate.command, "--goto", location]
    if candidate.id == 'zed':
        return [*candidate.command, str(path)]
    if candidate.id == 'sublime':
        return [*candidate.command, location]
    if candidate.id == 'jetbrains':
        return [*candidate.command, str(path)]
    if candidate.id in TERMINAL_IDS:
        editor = _terminal_editor_command()
        if not editor:
            return [*candidate.command, str(path)]
        return [*candidate.command, "-e", *editor, f"+{line}", str(path)]
    if candidate.id == 'system':
        return [*candidate.command, str(path)]
    return [*candidate.command, str(path)]


def _build_app_open_command(candidate: IdeCandidate, path: Path, location: str, *, line: int) -> list[str]:
    if candidate.id in {'trae', 'cursor', 'code', 'windsurf'}:
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
