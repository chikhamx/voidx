from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voidx.config import McpServerConfig
from voidx.mcp.description_cache import McpDescriptionCache, description_fingerprint
from voidx.tooling.adapters.mcp_description_generator import McpDescriptionBatch, McpDescriptionGenerator
from voidx.mcp.schema import McpToolDef


class StructuredFakeModel:
    def __init__(self, descriptions: dict[str, str], error: Exception | None = None) -> None:
        self.descriptions = descriptions
        self.error = error
        self.calls = []
        self.schema = None
        self.structured_kwargs = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.structured_kwargs = kwargs
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return McpDescriptionBatch(descriptions=self.descriptions)


@pytest.mark.asyncio
async def test_description_generator_batches_servers_and_parses_json():
    model = StructuredFakeModel({"tavily": "Search the web for current information."})
    generator = McpDescriptionGenerator(model)

    result = await generator.generate({
        "tavily": [McpToolDef(name="search", description="Search the web")],
        "github": [McpToolDef(name="search_code", description="Search code")],
    })

    assert result == {"tavily": "Search the web for current information."}
    assert len(model.calls) == 1
    assert model.schema["function"]["name"] == "McpDescriptionBatch"
    assert "$defs" not in model.schema["function"]["parameters"]
    assert model.structured_kwargs == {"method": "function_calling"}
    prompt = str(model.calls[0][-1].content)
    assert "tavily" in prompt
    assert "github" in prompt
    assert "Search the web" in prompt


@pytest.mark.asyncio
async def test_description_generator_propagates_structured_output_failure():
    generator = McpDescriptionGenerator(StructuredFakeModel({}, ValueError("bad output")))

    with pytest.raises(ValueError, match="bad output"):
        await generator.generate({"tavily": [McpToolDef(name="search")]})


def test_description_cache_persists_by_fingerprint(tmp_path):
    cache = McpDescriptionCache(str(tmp_path))
    cache.put("tavily", "fingerprint-a", "Search the web.")

    reloaded = McpDescriptionCache(str(tmp_path))

    assert reloaded.get("tavily", "fingerprint-a") == "Search the web."
    assert reloaded.get("tavily", "fingerprint-b") is None


def test_description_fingerprint_changes_when_tool_metadata_changes():
    server = McpServerConfig(name="tavily", command="fake")
    first = description_fingerprint(
        server,
        [McpToolDef(name="search", description="Search the web")],
    )
    second = description_fingerprint(
        server,
        [McpToolDef(name="search", description="Search news and pages")],
    )

    assert first != second
