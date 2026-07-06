from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from voidx.agent.slash.init import INIT_PROMPT
from voidx.ui.commands import COMMANDS


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.agent.slash.init.ui.print",
        lambda text="": output.append(str(text)),
    )
    monkeypatch.setattr(
        "voidx.agent.slash.init.ui.error",
        lambda text="": output.append(f"ERROR: {text}"),
    )
    return output


def _graph(tmp_path, *, plan_mode: bool = False):
    calls: list[tuple[str, str | None]] = []

    async def run_synthetic_turn(text: str, *, display_text: str | None = None) -> None:
        calls.append((text, display_text))

    graph = SimpleNamespace(
        _workspace=str(tmp_path),
        _plan_mode=plan_mode,
        run_synthetic_turn=run_synthetic_turn,
    )
    return graph, calls


@pytest.mark.asyncio
async def test_init_dispatches_synthetic_turn(tmp_path):
    graph, calls = _graph(tmp_path)

    assert await SlashHandler(graph).dispatch("/init") is True

    assert calls == [(INIT_PROMPT, "/init")]


@pytest.mark.asyncio
async def test_init_refuses_existing_agents_file_without_force(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    graph, calls = _graph(tmp_path)

    assert await SlashHandler(graph).dispatch("/init") is True

    assert calls == []
    assert output == ["[dim]AGENTS.md already exists. Use /init force to regenerate.[/dim]"]


@pytest.mark.asyncio
async def test_init_force_allows_existing_agents_file(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    graph, calls = _graph(tmp_path)

    assert await SlashHandler(graph).dispatch("/init force") is True

    assert calls == [(INIT_PROMPT, "/init")]


@pytest.mark.asyncio
async def test_init_rejects_invalid_args(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    graph, calls = _graph(tmp_path)

    assert await SlashHandler(graph).dispatch("/init now") is True

    assert calls == []
    assert output == ["ERROR: Usage: /init [force]"]


@pytest.mark.asyncio
async def test_init_rejects_plan_mode(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    graph, calls = _graph(tmp_path, plan_mode=True)

    assert await SlashHandler(graph).dispatch("/init") is True

    assert calls == []
    assert output == ["ERROR: /init writes AGENTS.md. Run /unplan first."]


def test_init_command_is_in_palette():
    assert ("/init", "Generate AGENTS.md for this project") in COMMANDS
    assert ("/init force", "Regenerate AGENTS.md even if it already exists") in COMMANDS
