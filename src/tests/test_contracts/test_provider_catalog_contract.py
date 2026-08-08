from __future__ import annotations

from voidx.llm.providers import all_specs

from .snapshot import assert_snapshot


def test_provider_catalog_contract() -> None:
    from voidx.llm.domain.model import ModelConfig
    from voidx.llm.domain.model import ReasoningEffort

    providers = []
    for spec in all_specs():
        model = spec.static_models[0] if spec.static_models else "contract-model"
        matrix = []
        for effort in ReasoningEffort:
            config = ModelConfig(
                provider=spec.name,
                model=model,
                reasoning_effort=effort,
            )
            matrix.append(
                {
                    "effort": effort.value,
                    "reasoning": spec.reasoning(config) if spec.reasoning else {},
                    "temperature_override": (
                        spec.temperature_override(config)
                        if spec.temperature_override
                        else None
                    ),
                }
            )
        providers.append(
            {
                "name": spec.name,
                "protocol": spec.protocol,
                "default_base_url": spec.default_base_url,
                "context_limit": spec.context_limit,
                "static_models": list(spec.static_models),
                "matrix": matrix,
            }
        )
    assert_snapshot("providers.json", providers)
