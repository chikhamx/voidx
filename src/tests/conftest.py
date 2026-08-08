from pathlib import Path

import pytest
import voidx.persistence.sqlite as store


@pytest.fixture(autouse=True)
def isolated_settings_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from voidx.config.adapters.profile_store import MemoryModelProfileStore
    from voidx.config.settings import Settings

    original_init = Settings.__init__
    original_create = Settings.create.__func__

    def test_init(self, workspace=".", *, profile_store=None):
        return original_init(
            self,
            workspace,
            profile_store=profile_store or MemoryModelProfileStore(),
        )

    async def test_create(cls, workspace=".", *, profile_store=None):
        return await original_create(
            cls,
            workspace,
            profile_store=profile_store or MemoryModelProfileStore(),
        )

    monkeypatch.setattr(Settings, "__init__", test_init)
    monkeypatch.setattr(Settings, "create", classmethod(test_create))
    monkeypatch.setattr("voidx.config.settings._settings_home", lambda: tmp_path)
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.fixture(autouse=True)
def simulated_llm_retry_sleep(monkeypatch: pytest.MonkeyPatch):
    def simulated_delay(_delay: float) -> float:
        return 0.002

    monkeypatch.setattr(
        "voidx.agent.adapters.langgraph.runtime.core.loop._llm_retry_sleep_delay",
        simulated_delay,
    )
    monkeypatch.setattr(
        "voidx.agent.adapters.langgraph.runtime.subagent._llm_retry_sleep_delay",
        simulated_delay,
    )
