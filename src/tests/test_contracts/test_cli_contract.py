from __future__ import annotations

import importlib
import re

from typer.main import get_command
from typer.testing import CliRunner

from voidx.main import cli

from .snapshot import assert_snapshot


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n")


def _metadata() -> list[dict[str, object]]:
    command = get_command(cli)
    return [
        {
            "name": param.name,
            "opts": list(param.opts),
            "default": param.default,
            "help": param.help,
            "is_flag": bool(getattr(param, "is_flag", False)),
            "required": param.required,
        }
        for param in command.params
        if param.name not in {"install_completion", "show_completion"}
    ]


def test_cli_contract(monkeypatch) -> None:
    class Console:
        def print(self, message) -> None:
            print(message)

        def error(self, message) -> None:
            print(message)

    async def no_session(*args, **kwargs):
        return None

    async def no_sessions(*args, **kwargs):
        return []

    async def fake_run_chat(*args, **kwargs):
        resume = args[3]
        if resume:
            await main_module._select_start_session(resume, Console())

    main_module = importlib.import_module("voidx.bootstrap.command_line")
    import voidx.agent.adapters.persistence.session_repository as sessions

    monkeypatch.setattr(main_module, "_print_version", lambda: print("voidx v${VERSION}"))
    monkeypatch.setattr(main_module, "_run_chat", fake_run_chat)
    monkeypatch.setattr(main_module, "_vconsole", Console)
    monkeypatch.setattr(sessions, "get_session", no_session)
    monkeypatch.setattr(sessions, "list_sessions", no_sessions)
    runner = CliRunner()
    cases = {}
    for name, argv in {
        "help": ["--help"],
        "version_option": ["--version"],
        "version_command": ["version"],
        "sessions_empty": ["sessions"],
        "web_headless_without_web": ["--web-headless"],
        "missing_resume": ["--resume", "missing-session"],
    }.items():
        result = runner.invoke(cli, argv)
        cases[name] = {
            "argv": argv,
            "exit_code": result.exit_code,
            "stdout": _clean(result.stdout),
            "stderr": _clean(result.stderr),
        }
    assert_snapshot(
        "cli.json",
        {"options": _metadata(), "commands": list(get_command(cli).commands), "cases": cases},
    )
