"""Tests for protocol v2 method registry and dispatch."""

from __future__ import annotations

import pytest

from voidx.ui.protocol.v2.envelope import (
    ERR_INTERNAL_ERROR,
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResult,
)
from voidx.ui.protocol.v2.methods import MethodDispatch, MethodParamsError


# ── registry ────────────────────────────────────────────────────────────


async def _handler_create(params: dict) -> dict:
    return {"thread_id": "t1", "title": params.get("title", "New session")}


async def _handler_submit(params: dict) -> dict:
    if "text" not in params:
        raise MethodParamsError("missing required param: text")
    return {"turn_id": "turn1"}


@pytest.mark.asyncio
async def test_register_and_dispatch_request_returns_result():
    dispatch = MethodDispatch()
    dispatch.register("session.create", _handler_create)

    req = JsonRpcRequest(id=1, method="session.create", params={"title": "My session"})
    msg = await dispatch.dispatch(req)

    assert isinstance(msg, JsonRpcResult)
    assert msg.id == 1
    assert msg.result == {"thread_id": "t1", "title": "My session"}


@pytest.mark.asyncio
async def test_dispatch_unknown_method_returns_error():
    dispatch = MethodDispatch()
    req = JsonRpcRequest(id=2, method="nonexistent.method", params={})
    msg = await dispatch.dispatch(req)

    assert isinstance(msg, JsonRpcError)
    assert msg.id == 2
    assert msg.error.code == ERR_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_dispatch_params_error_returns_invalid_params():
    dispatch = MethodDispatch()
    dispatch.register("agent.submit", _handler_submit)

    req = JsonRpcRequest(id=3, method="agent.submit", params={})
    msg = await dispatch.dispatch(req)

    assert isinstance(msg, JsonRpcError)
    assert msg.id == 3
    assert msg.error.code == ERR_INVALID_PARAMS


@pytest.mark.asyncio
async def test_dispatch_handler_exception_returns_internal_error():
    async def _bad_handler(params: dict) -> dict:
        raise RuntimeError("boom")

    dispatch = MethodDispatch()
    dispatch.register("bad.method", _bad_handler)

    req = JsonRpcRequest(id=4, method="bad.method", params={})
    msg = await dispatch.dispatch(req)

    assert isinstance(msg, JsonRpcError)
    assert msg.id == 4
    assert msg.error.code == ERR_INTERNAL_ERROR
    assert "boom" in msg.error.message


@pytest.mark.asyncio
async def test_dispatch_sync_handler_supported():
    def _sync_handler(params: dict) -> dict:
        return {"ok": True}

    dispatch = MethodDispatch()
    dispatch.register("sync.method", _sync_handler)

    req = JsonRpcRequest(id=5, method="sync.method", params={})
    msg = await dispatch.dispatch(req)

    assert isinstance(msg, JsonRpcResult)
    assert msg.result == {"ok": True}


def test_registered_methods_listed():
    dispatch = MethodDispatch()
    dispatch.register("session.create", _handler_create)
    dispatch.register("agent.submit", _handler_submit)

    methods = dispatch.registered_methods()
    assert "session.create" in methods
    assert "agent.submit" in methods
