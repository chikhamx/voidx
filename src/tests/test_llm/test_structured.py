import asyncio

import pytest
from pydantic import BaseModel

from voidx.llm.structured import ainvoke_structured


class Result(BaseModel):
    value: str


class NestedItem(BaseModel):
    name: str


class NestedResult(BaseModel):
    items: list[NestedItem]


class FakeRunnable:
    async def ainvoke(self, messages):
        return Result(value=str(messages[0]))


class FakeModel:
    def __init__(self, method=None, *, accepts_kwargs=True):
        self.resolver_structured_output_method = method
        self.calls = []
        self.accepts_kwargs = accepts_kwargs

    def with_structured_output(self, schema, **kwargs):
        self.calls.append((schema, kwargs))
        return FakeRunnable()


@pytest.mark.asyncio
async def test_ainvoke_structured_defaults_to_function_calling():
    model = FakeModel()

    result = await ainvoke_structured(
        model=model,
        schema=Result,
        messages=["hello"],
    )

    assert result.value == "hello"
    schema, kwargs = model.calls[0]
    assert kwargs == {"method": "function_calling"}
    assert schema["function"]["name"] == "Result"
    assert schema["function"]["parameters"]["properties"]["value"]["type"] == "string"


@pytest.mark.asyncio
async def test_ainvoke_structured_uses_flat_function_schema():
    model = FakeModel()

    await ainvoke_structured(
        model=model,
        schema=NestedResult,
        messages=["hello"],
    )

    schema, kwargs = model.calls[0]
    assert kwargs == {"method": "function_calling"}
    assert "$defs" not in schema["function"]["parameters"]
    assert schema["function"]["parameters"]["properties"]["items"]["items"]["properties"]["name"]["type"] == "string"


@pytest.mark.asyncio
async def test_ainvoke_structured_preserves_explicit_method_and_include_raw():
    model = FakeModel()

    await ainvoke_structured(
        model=model,
        schema=Result,
        messages=["hello"],
        method="json_mode",
        include_raw=True,
    )

    assert model.calls == [(Result, {"method": "json_mode", "include_raw": True})]


@pytest.mark.asyncio
async def test_ainvoke_structured_applies_timeout():
    class SlowRunnable:
        async def ainvoke(self, _messages):
            await asyncio.sleep(1)

    class SlowModel:
        def with_structured_output(self, _schema, **_kwargs):
            return SlowRunnable()

    with pytest.raises(asyncio.TimeoutError):
        await ainvoke_structured(
            model=SlowModel(),
            schema=Result,
            messages=[],
            timeout=0.001,
        )


@pytest.mark.asyncio
async def test_ainvoke_structured_rejects_missing_support():
    with pytest.raises(RuntimeError, match="structured output"):
        await ainvoke_structured(model=object(), schema=Result, messages=[])
