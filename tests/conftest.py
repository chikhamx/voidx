from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_settings_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("voidx.config.settings._settings_home", lambda: tmp_path)
