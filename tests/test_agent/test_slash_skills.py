import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.config import Settings


def _write_skill(workspace: Path, name: str, body: str = "Skill body") -> None:
    skill_dir = workspace / ".voidx" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} helper\n---\n{body}",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_skills_dispatch_lists_and_shows_project_skill(tmp_path):
    _write_skill(tmp_path, "docs", "Write docs clearly.")
    graph = SimpleNamespace(_settings=Settings(str(tmp_path)), _workspace=str(tmp_path))

    handler = SlashHandler(graph)

    assert await handler.dispatch("/skills") is True
    assert await handler.dispatch("/skills show docs") is True


@pytest.mark.asyncio
async def test_skills_enable_disable_updates_settings(tmp_path):
    _write_skill(tmp_path, "docs")
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(_settings=settings, _workspace=str(tmp_path))
    handler = SlashHandler(graph)

    assert await handler.dispatch("/skills disable docs") is True
    selection = Settings(str(tmp_path)).get_skill_selection()
    assert selection.disabled == {"docs"}

    assert await handler.dispatch("/skills enable docs") is True
    selection = Settings(str(tmp_path)).get_skill_selection()
    assert selection.disabled == set()
    assert selection.enabled == {"docs"}


@pytest.mark.asyncio
async def test_skills_paths_dispatch(tmp_path):
    graph = SimpleNamespace(_settings=Settings(str(tmp_path)), _workspace=str(tmp_path))

    assert await SlashHandler(graph).dispatch("/skills paths") is True


@pytest.mark.asyncio
async def test_skills_paths_prints_bundled_source(tmp_path, monkeypatch):
    output: list[str] = []
    monkeypatch.setattr("voidx.agent.slash.skills.ui.print", lambda text="": output.append(str(text)))
    graph = SimpleNamespace(_settings=Settings(str(tmp_path)), _workspace=str(tmp_path))

    assert await SlashHandler(graph).dispatch("/skills paths") is True

    rendered = "\n".join(output)
    assert "bundled" in rendered
    assert "superpowers" in rendered
