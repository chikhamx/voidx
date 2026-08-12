from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

from voidx.config import CompactionConfig, Settings
from voidx.llm.domain.model import ReasoningEffort


def test_compaction_config_defaults_preserve_inherit() -> None:
    config = CompactionConfig()

    assert config.profile_name == ""
    assert config.reasoning_effort is None
    assert config.timeout_seconds == 256.0
    assert config.model_dump(mode="json")["reasoning_effort"] is None


@pytest.mark.parametrize("timeout", [0, 301, math.inf, -math.inf, math.nan])
def test_compaction_config_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        CompactionConfig(timeout_seconds=timeout)


def test_compaction_config_distinguishes_none_effort_from_inherit() -> None:
    inherited = CompactionConfig(reasoning_effort=None)
    disabled = CompactionConfig(reasoning_effort=ReasoningEffort.NONE)

    assert inherited.reasoning_effort is None
    assert disabled.reasoning_effort is ReasoningEffort.NONE


def test_compaction_settings_round_trip_is_workspace_only(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr("voidx.config.settings._settings_home", lambda: home)
    settings = Settings(str(workspace))

    path = settings.set_compaction_config(
        CompactionConfig(
            profile_name="openai/gpt-5",
            reasoning_effort=None,
            timeout_seconds=45,
        )
    )

    assert path == workspace / ".voidx" / "settings.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["compaction"] == {
        "profile_name": "openai/gpt-5",
        "reasoning_effort": None,
        "timeout_seconds": 45.0,
    }
    assert not (home / ".voidx" / "settings.json").exists()
    assert Settings(str(workspace)).get_compaction_config() == CompactionConfig(
        profile_name="openai/gpt-5",
        reasoning_effort=None,
        timeout_seconds=45,
    )


@pytest.mark.parametrize(
    "raw",
    [None, [], "bad", {"reasoning_effort": "invalid"}, {"timeout_seconds": 0}],
)
def test_invalid_persisted_compaction_config_warns_and_defaults(
    tmp_path, caplog, raw
) -> None:
    settings = Settings(str(tmp_path))
    settings._set_setting("compaction", raw)

    with caplog.at_level("WARNING", logger="voidx.config.settings_compaction"):
        config = settings.get_compaction_config()

    assert config == CompactionConfig()
    assert "Invalid compaction settings" in caplog.text
