from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from voidx.config import CompactionConfig, Settings
from voidx.llm.domain.model import ModelConfig, ReasoningEffort
from voidx.presentation.slash import SlashHandler
from tests.test_slash.context import command_context


class RecordingUi:
    def __init__(self) -> None:
        self.output: list[str] = []
        self.errors: list[str] = []

    def print(self, text: str = "") -> None:
        self.output.append(str(text))

    def error(self, text: str) -> None:
        self.errors.append(str(text))


async def _handler(tmp_path, *, profiles: set[str] | None = None):
    settings = Settings(str(tmp_path))
    ui = RecordingUi()

    async def resolve_profile(name: str):
        return SimpleNamespace(name=name) if name in (profiles or set()) else None

    settings.resolve_profile = resolve_profile
    graph = command_context(
        settings=settings,
        ui=ui,
        config=SimpleNamespace(model=ModelConfig(reasoning_effort=ReasoningEffort.HIGH)),
    )
    return SlashHandler(graph), settings, ui


@pytest.mark.asyncio
async def test_compact_model_query_shows_stored_and_effective_values(tmp_path) -> None:
    handler, settings, ui = await _handler(tmp_path)
    settings.set_compaction_config(CompactionConfig(reasoning_effort=None, timeout_seconds=42))

    assert await handler.dispatch("/compact-model") is True

    rendered = "\n".join(ui.output)
    assert "stored profile: inherit" in rendered
    assert "effective profile: anthropic/claude-sonnet-4-6" in rendered
    assert "stored reasoning: inherit" in rendered
    assert "effective reasoning: high" in rendered
    assert "reasoning source: main" in rendered
    assert "timeout: 42" in rendered


@pytest.mark.asyncio
async def test_compact_model_profile_and_clear_preserve_reasoning(tmp_path) -> None:
    profile = "openai/gpt-5"
    handler, settings, _ui = await _handler(tmp_path, profiles={profile})
    settings.set_compaction_config(
        CompactionConfig(reasoning_effort=ReasoningEffort.NONE, timeout_seconds=25)
    )

    await handler.dispatch(f"/compact-model {profile}")
    configured = settings.get_compaction_config()
    assert configured.profile_name == profile
    assert configured.reasoning_effort is ReasoningEffort.NONE
    assert configured.timeout_seconds == 25

    await handler.dispatch("/compact-model clear")
    cleared = settings.get_compaction_config()
    assert cleared.profile_name == ""
    assert cleared.reasoning_effort is ReasoningEffort.NONE
    assert cleared.timeout_seconds == 25


@pytest.mark.asyncio
async def test_compact_model_timeout_and_reasoning_updates_are_independent(tmp_path) -> None:
    handler, settings, ui = await _handler(tmp_path)

    await handler.dispatch("/compact-model timeout 12.5")
    await handler.dispatch("/compact-model reasoning none")
    assert settings.get_compaction_config() == CompactionConfig(
        reasoning_effort=ReasoningEffort.NONE,
        timeout_seconds=12.5,
    )

    await handler.dispatch("/compact-model reasoning")
    assert "stored reasoning: none" in "\n".join(ui.output)
    assert "reasoning source: compaction" in "\n".join(ui.output)

    await handler.dispatch("/compact-model reasoning inherit")
    assert settings.get_compaction_config().reasoning_effort is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "/compact-model missing/profile",
        "/compact-model timeout 0",
        "/compact-model timeout nan",
        "/compact-model timeout 301",
        "/compact-model timeout nope",
        "/compact-model reasoning invalid",
        "/compact-model unknown extra",
    ],
)
async def test_compact_model_invalid_input_does_not_persist(tmp_path, command: str) -> None:
    handler, settings, ui = await _handler(tmp_path)
    settings.set_compaction_config(
        CompactionConfig(profile_name="existing/profile", timeout_seconds=33)
    )
    path = tmp_path / ".voidx" / "settings.json"
    before = path.read_bytes()

    await handler.dispatch(command)

    assert path.read_bytes() == before
    assert ui.errors
    assert json.loads(before)["compaction"]["profile_name"] == "existing/profile"
