import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, "src")

from voidx.config import CodeIde
from voidx.ui.tools.code_ide import IdeCandidate, build_open_command, choose_ide, normalize_ide


def test_build_open_command_uses_goto_for_trae_cli():
    candidate = IdeCandidate(
        id=CodeIde.TRAE.value,
        label="Trae",
        command=["/usr/local/bin/trae"],
        source="cli:trae",
        available=True,
    )

    command = build_open_command(candidate, PurePosixPath("/tmp/app.py"), line=12)

    assert command == ["/usr/local/bin/trae", "--goto", "/tmp/app.py:12"]


def test_build_open_command_uses_goto_for_trae_app():
    candidate = IdeCandidate(
        id=CodeIde.TRAE.value,
        label="Trae",
        command=["open", "-a", "Trae"],
        source="app:/Applications/Trae.app",
        available=True,
    )

    command = build_open_command(candidate, PurePosixPath("/tmp/app.py"), line=12)

    assert command == ["open", "-a", "Trae", "--args", "--goto", "/tmp/app.py:12"]


def test_build_open_command_uses_editor_inside_ghostty(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim -N")
    candidate = IdeCandidate(
        id=CodeIde.GHOSTTY.value,
        label="Ghostty",
        command=["/usr/local/bin/ghostty"],
        source="cli:ghostty",
        available=True,
    )

    command = build_open_command(candidate, PurePosixPath("/tmp/app.py"), line=12)

    assert command == ["/usr/local/bin/ghostty", "-e", "vim", "-N", "+12", "/tmp/app.py"]


def test_choose_ide_prefers_configured_trae_when_detected():
    detected = [
        IdeCandidate(CodeIde.CURSOR.value, "Cursor", ["cursor"], "cli:cursor", True),
        IdeCandidate(CodeIde.TRAE.value, "Trae", ["trae"], "cli:trae", True),
    ]

    selected = choose_ide(detected=detected, preferred=CodeIde.TRAE)

    assert selected is not None
    assert selected.id == CodeIde.TRAE.value


def test_gostty_typo_is_not_normalized_to_ghostty():
    detected = [
        IdeCandidate(CodeIde.CURSOR.value, "Cursor", ["cursor"], "cli:cursor", True),
        IdeCandidate(CodeIde.GHOSTTY.value, "Ghostty", ["ghostty"], "cli:ghostty", True),
    ]

    selected = choose_ide(detected=detected, preferred="gostty")

    assert normalize_ide("gostty") == "gostty"
    assert selected is not None
    assert selected.id == CodeIde.CURSOR.value
