import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from tests.test_slash.context import command_context
from voidx.bootstrap.skills import build_skills_api
from voidx.config import Settings


def _write_skill(workspace: Path, name: str, body: str = "Skill body") -> None:
    skill_dir = workspace / ".voidx" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} helper\n---\n{body}",
        encoding="utf-8",
    )



def _skills_context(workspace: Path, settings: Settings):
    context = command_context(settings=settings, workspace=str(workspace))
    context.skills_api = build_skills_api(str(workspace), settings)

    def invalidate() -> None:
        context.skills_api = build_skills_api(str(workspace), settings)

    context.invalidate_skill_service_cache = invalidate
    return context

@pytest.mark.asyncio
async def test_skills_dispatch_lists_and_shows_project_skill(tmp_path):
    _write_skill(tmp_path, "docs", "Write docs clearly.")
    graph = _skills_context(tmp_path, Settings(str(tmp_path)))

    handler = SlashHandler(graph)

    assert await handler.dispatch("/skills") is True
    assert await handler.dispatch("/skills show docs") is True


@pytest.mark.asyncio
async def test_skills_enable_disable_updatessettings(tmp_path):
    _write_skill(tmp_path, "docs")
    settings = Settings(str(tmp_path))
    graph = _skills_context(tmp_path, settings)
    handler = SlashHandler(graph)

    assert await handler.dispatch("/skills disable docs") is True
    selection = Settings(str(tmp_path)).get_skill_selection()
    assert selection.disabled == {"docs"}

    assert await handler.dispatch("/skills enable docs") is True
    selection = Settings(str(tmp_path)).get_skill_selection()
    assert selection.disabled == set()
    assert selection.enabled == {"docs"}


@pytest.mark.asyncio
async def test_skills_auto_manual_updatessettings(tmp_path):
    _write_skill(tmp_path, "docs")
    settings = Settings(str(tmp_path))
    graph = _skills_context(tmp_path, settings)
    handler = SlashHandler(graph)

    assert await handler.dispatch("/skills auto docs") is True
    selection = Settings(str(tmp_path)).get_skill_selection()
    assert selection.enabled == {"docs"}
    assert selection.disabled == set()
    assert selection.auto == {"docs"}

    assert await handler.dispatch("/skills manual docs") is True
    selection = Settings(str(tmp_path)).get_skill_selection()
    assert selection.enabled == {"docs"}
    assert selection.auto == set()


@pytest.mark.asyncio
async def test_skills_paths_dispatch(tmp_path):
    graph = _skills_context(tmp_path, Settings(str(tmp_path)))

    assert await SlashHandler(graph).dispatch("/skills paths") is True


@pytest.mark.asyncio
async def test_skills_paths_prints_bundled_source(tmp_path, monkeypatch):
    output: list[str] = []
    monkeypatch.setattr("voidx.agent.slash.handler.ui.print", lambda text="": output.append(str(text)))
    graph = _skills_context(tmp_path, Settings(str(tmp_path)))

    assert await SlashHandler(graph).dispatch("/skills paths") is True

    rendered = "\n".join(output)
    assert "bundled" in rendered


from voidx.presentation.commands import COMMANDS


def test_skills_subcommands_are_in_palette():
    assert ("/skills", "Manage local skills") in COMMANDS
    assert ("/skills list", "List local skills") in COMMANDS
    assert ("/skills show", "Show a skill's content") in COMMANDS
    assert ("/skills enable", "Enable a skill") in COMMANDS
    assert ("/skills disable", "Disable a skill") in COMMANDS
    assert ("/skills auto", "Set a skill to auto-trigger") in COMMANDS
    assert ("/skills manual", "Set a skill to manual trigger") in COMMANDS
    assert ("/skills paths", "Show skill directory paths") in COMMANDS
