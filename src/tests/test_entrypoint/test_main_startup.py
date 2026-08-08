from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.config import Config, Profile
from voidx.llm.domain.model import ModelConfig
from voidx.main import _run_chat


@pytest.mark.asyncio
async def test_run_chat_resolves_profile_once(monkeypatch, tmp_path):
    profile = Profile(name="mimo/test-model", api_key="profile-key")
    captured = SimpleNamespace(profile_calls=0, build_profile=None, api_key=None)

    class FakeSettings:
        @classmethod
        async def create(cls, workspace: str, **kwargs):
            return cls()

        async def resolve_profile(self):
            captured.profile_calls += 1
            return profile

        async def build_config(self, *, profile=None):
            captured.build_profile = profile
            return Config(model=ModelConfig(provider="mimo", model="test-model"))

        async def resolve_api_key(self, provider: str):
            raise AssertionError("resolve_api_key should not be called for matching profile")

    class FakeAgentApp:
        async def run(self, **kwargs):
            return None

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    def fake_build_agent_app(cfg, api_key, **kwargs):
        captured.api_key = api_key
        return FakeAgentApp()

    monkeypatch.setattr("voidx.bootstrap.agent.build_agent_app", fake_build_agent_app)
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
        async def create(cls, workspace: str, **kwargs):
            return cls()

        async def resolve_profile(self):
            return None

        async def build_config(self, *, profile=None):
            return Config(model=ModelConfig(provider="openai", model="gpt-test"))

        async def resolve_api_key(self, provider: str):
            captured.provider = provider
            return "resolved-key"

    class FakeAgentApp:
        async def run(self, **kwargs):
            return None

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    def fake_build_agent_app(cfg, api_key, **kwargs):
        captured.api_key = api_key
        return FakeAgentApp()

    monkeypatch.setattr("voidx.bootstrap.agent.build_agent_app", fake_build_agent_app)
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
        async def create(cls, workspace: str, **kwargs):
            return cls()

        async def resolve_profile(self):
            return profile

        async def build_config(self, *, profile=None):
            return Config(model=ModelConfig(provider="anthropic", model="claude-test"))

        async def resolve_api_key(self, provider: str):
            captured.provider = provider
            return "openai-key"

    class FakeAgentApp:
        async def run(self, **kwargs):
            return None

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    def fake_build_agent_app(cfg, api_key, **kwargs):
        captured.api_key = api_key
        return FakeAgentApp()

    monkeypatch.setattr("voidx.bootstrap.agent.build_agent_app", fake_build_agent_app)
    monkeypatch.setattr("voidx.main._select_start_session", fake_select_start_session)

    await _run_chat(workspace=str(tmp_path), provider="openai")

    assert captured.provider == "openai"
    assert captured.api_key == "openai-key"
