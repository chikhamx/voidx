import httpx
import pytest

from voidx.llm.application.model_catalog import ModelCatalog
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.catalog import PROVIDER_SPECS


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.HTTPError("upstream failed"), RuntimeError("bug")])
async def test_list_models_fetcher_failure_falls_back(error):
    provider = "failure-provider"

    async def failing_fetcher():
        raise error

    spec = ProviderSpec(
        name=provider,
        protocol="openai",
        static_models=("static-model",),
    )
    catalog = ModelCatalog(
        provider_specs=(spec,),
        fetchers={provider: failing_fetcher},
    )

    assert await catalog.list_models(provider) == ["static-model"]


def test_xunfei_coding_plan_static_models():
    spec = next(item for item in PROVIDER_SPECS if item.name == "xunfei-coding-plan")
    assert "astron-code-latest" in spec.static_models
