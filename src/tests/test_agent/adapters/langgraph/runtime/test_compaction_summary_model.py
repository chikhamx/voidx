from types import SimpleNamespace

import pytest

from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
from voidx.config import CompactionConfig, Profile
from voidx.llm.domain.model import ModelConfig, ReasoningEffort


class _Settings:
    def __init__(self, config: CompactionConfig, profile: Profile | None = None) -> None:
        self.config = config
        self.profile = profile

    def get_compaction_config(self) -> CompactionConfig:
        return self.config

    async def resolve_profile(self, name: str):
        return self.profile if self.profile and self.profile.name == name else None


def _host(config: CompactionConfig, *, profile: Profile | None = None, main_model=object()):
    created = []

    def factory(api_key, model_config):
        model = object()
        created.append((api_key, model_config, model))
        return model

    host = SimpleNamespace(
        config=SimpleNamespace(
            model=ModelConfig(
                provider="anthropic",
                model="claude-main",
                reasoning_effort=ReasoningEffort.HIGH,
            )
        ),
        model=main_model,
        api_key="test-key",
        _settings=_Settings(config, profile),
        _model_factory=factory,
    )
    return host, created


@pytest.mark.asyncio
async def test_zero_config_reuses_exact_main_model() -> None:
    host, created = _host(CompactionConfig())

    coordinator = CompactionCoordinator(host)
    stages = await coordinator.resolve_compaction_models()

    assert len(stages) == 1
    assert stages[0].model is host.model
    assert stages[0].model_source == "main"
    assert stages[0].reasoning_source == "main"
    assert coordinator._compaction_config().timeout_seconds == 256.0
    assert created == []


def test_missing_compaction_settings_use_256_second_timeout() -> None:
    host, _ = _host(CompactionConfig())
    host._settings = SimpleNamespace()

    assert CompactionCoordinator(host)._compaction_config().timeout_seconds == 256.0


@pytest.mark.asyncio
async def test_reasoning_override_builds_temporary_main_model_then_main_fallback() -> None:
    host, created = _host(CompactionConfig(reasoning_effort=ReasoningEffort.LOW))

    stages = await CompactionCoordinator(host).resolve_compaction_models()

    assert len(stages) == 2
    assert stages[0].model is created[0][2]
    assert stages[0].model_config.provider == "anthropic"
    assert stages[0].model_config.model == "claude-main"
    assert stages[0].model_config.reasoning_effort is ReasoningEffort.LOW
    assert stages[0].reasoning_source == "compaction"
    assert stages[1].model is host.model
    assert host.config.model.reasoning_effort is ReasoningEffort.HIGH


@pytest.mark.asyncio
async def test_profile_uses_profile_credentials_and_inherits_main_reasoning() -> None:
    profile = Profile(
        name="openai/gpt-summary",
        api_key="test-key",
        base_url="https://summary.example/v1",
        protocol="openai",
    )
    host, created = _host(CompactionConfig(profile_name=profile.name), profile=profile)

    stages = await CompactionCoordinator(host).resolve_compaction_models()

    assert len(stages) == 2
    assert created[0][0] == "test-key"
    assert stages[0].model_config == ModelConfig(
        provider="openai",
        model="gpt-summary",
        base_url="https://summary.example/v1",
        protocol="openai",
        reasoning_effort=ReasoningEffort.HIGH,
    )
    assert stages[0].model_source == "profile"
    assert stages[0].reasoning_source == "main"
    assert stages[1].model is host.model


@pytest.mark.asyncio
async def test_invalid_profile_still_applies_reasoning_override_to_main_model() -> None:
    host, created = _host(
        CompactionConfig(
            profile_name="missing/model",
            reasoning_effort=ReasoningEffort.NONE,
        )
    )

    stages = await CompactionCoordinator(host).resolve_compaction_models()

    assert stages[0].model is created[0][2]
    assert stages[0].model_config.reasoning_effort is ReasoningEffort.NONE
    assert stages[1].model is host.model
