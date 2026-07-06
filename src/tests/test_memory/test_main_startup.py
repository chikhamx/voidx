from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.config import Config, ModelConfig, Profile
from voidx.main import _run_chat


@pytest.mark.asyncio
async def test_run_chat_resolves_profile_once(monkeypatch, tmp_path):
    profile = Profile(name="mimo/test-model", api_key="profile-key")
    captured = SimpleNamespace(profile_calls=0, build_profile=None, api_key=None)

    class FakeSettings:
        @classmethod
        async def create(cls, workspace: str):
            return cls()

        async def resolve_profile(self):
            captured.profile_calls += 1
            return profile

        async def build_config(self, *, profile=None):
            captured.build_profile = profile
            return Config(model=ModelConfig(provider="mimo", model="test-model"))

        async def resolve_api_key(self, provider: str):
            raise AssertionError("resolve_api_key should not be called for matching profile")

    class FakeGraph:
        def __init__(self, cfg, api_key, *, session=None, settings=None):
            captured.api_key = api_key

        async def run(self, **kwargs):
            return None

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    monkeypatch.setattr("voidx.agent.graph.VoidXGraph", FakeGraph)
    monkeypatch.setattr("voidx.main._select_start_session", fake_select_start_session)

    await _run_chat(workspace=str(tmp_path))

    assert captured.profile_calls == 1
    assert captured.build_profile is profile
    assert captured.api_key == "profile-key"


@pytest.mark.asyncio
async def test_run_chat_awaits_resolve_api_key_when_no_profile(monkeypatch, tmp_path):
    captured = SimpleNamespace(api_key=None, provider="")

    class FakeSettings:
        @classmethod
        async def create(cls, workspace: str):
            return cls()

        async def resolve_profile(self):
            return None

        async def build_config(self, *, profile=None):
            return Config(model=ModelConfig(provider="openai", model="gpt-test"))

        async def resolve_api_key(self, provider: str):
            captured.provider = provider
            return "resolved-key"

    class FakeGraph:
        def __init__(self, cfg, api_key, *, session=None, settings=None):
            captured.api_key = api_key

        async def run(self, **kwargs):
            return None

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    monkeypatch.setattr("voidx.agent.graph.VoidXGraph", FakeGraph)
    monkeypatch.setattr("voidx.main._select_start_session", fake_select_start_session)

    await _run_chat(workspace=str(tmp_path))

    assert captured.provider == "openai"
    assert captured.api_key == "resolved-key"


@pytest.mark.asyncio
async def test_run_chat_uses_provider_specific_key_after_cli_override(monkeypatch, tmp_path):
    profile = Profile(name="anthropic/claude-test", api_key="anthropic-key")
    captured = SimpleNamespace(api_key=None, provider="")

    class FakeSettings:
        @classmethod
        async def create(cls, workspace: str):
            return cls()

        async def resolve_profile(self):
            return profile

        async def build_config(self, *, profile=None):
            return Config(model=ModelConfig(provider="anthropic", model="claude-test"))

        async def resolve_api_key(self, provider: str):
            captured.provider = provider
            return "openai-key"

    class FakeGraph:
        def __init__(self, cfg, api_key, *, session=None, settings=None):
            captured.api_key = api_key

        async def run(self, **kwargs):
            return None

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    monkeypatch.setattr("voidx.agent.graph.VoidXGraph", FakeGraph)
    monkeypatch.setattr("voidx.main._select_start_session", fake_select_start_session)

    await _run_chat(workspace=str(tmp_path), provider="openai")

    assert captured.provider == "openai"
    assert captured.api_key == "openai-key"
