"""usage.get RPC — exposes UsageStats snapshot to desktop/web frontends."""

from __future__ import annotations

import pytest

from voidx.presentation.gateway.session.core import GatewaySession
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult


@pytest.mark.asyncio
async def test_usage_get_returns_usage_snapshot():
    from voidx.llm.usage import UsageStats

    dock = BottomInputDock()
    stats = UsageStats()
    stats.context_tokens = 42_000
    stats.context_limit = 200_000
    stats.total_input_tokens = 100_000
    stats.total_output_tokens = 5_000
    stats.total_cache_read_tokens = 60_000
    stats.total_cache_metric_calls = 3

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        usage_stats_provider=lambda: stats,
    )

    result = await session.dispatch_request(JsonRpcRequest(id=1, method="usage.get", params={}))

    assert isinstance(result, JsonRpcResult)
    usage = result.result["usage"]
    assert usage["context_tokens"] == 42_000
    assert usage["context_limit"] == 200_000
    assert usage["total_tokens"] == 105_000
    assert usage["cache_hit_rate"] == pytest.approx(60_000 / 100_000)
    assert usage["cache_hit_rate_estimated"] is False


@pytest.mark.asyncio
async def test_usage_get_without_provider_returns_empty_usage():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    result = await session.dispatch_request(JsonRpcRequest(id=2, method="usage.get", params={}))

    assert isinstance(result, JsonRpcResult)
    assert result.result["usage"] == {}


@pytest.mark.asyncio
async def test_usage_get_null_cache_rate_when_no_data():
    from voidx.llm.usage import UsageStats

    dock = BottomInputDock()
    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        usage_stats_provider=UsageStats,
    )

    result = await session.dispatch_request(JsonRpcRequest(id=3, method="usage.get", params={}))

    assert isinstance(result, JsonRpcResult)
    usage = result.result["usage"]
    assert usage["cache_hit_rate"] is None
    assert usage["context_tokens"] == 0
