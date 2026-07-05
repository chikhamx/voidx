from pathlib import Path

import pytest
from voidx.memory import store


@pytest.fixture(autouse=True)
def isolated_settings_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("voidx.config.settings._settings_home", lambda: tmp_path)
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None
