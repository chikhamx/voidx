"""Tests for the integrated terminal PTY manager.

The terminal manager provides:
- create: spawn a PTY process, return terminal_id
- write: send input to the PTY
- resize: adjust PTY dimensions
- close: terminate the PTY process
- read: async iterator for PTY output

Platform abstraction:
- Windows: pywinpty
- Unix: os.forkpty / pty module
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


from voidx.ui.gateway.terminal import TerminalManager, TerminalOutput, TerminalSession

# Command that prints a known string and exits
ECHO_CMD = [sys.executable, "-c", "print('hello')"]


# ── TerminalManager basic lifecycle ────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_manager_create_does_not_warn_on_unix_pty():
    if sys.platform == "win32":
        pytest.skip("Unix PTY implementation only")

    import warnings

    manager = TerminalManager()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session = await manager.create(command=ECHO_CMD)

    await manager.close(session.terminal_id)
    deprecations = [warning for warning in caught if warning.category is DeprecationWarning]
    details = [(warning.filename, warning.lineno, str(warning.message)) for warning in deprecations]
    assert not deprecations, details


@pytest.mark.asyncio
async def test_terminal_manager_create_returns_session():
    manager = TerminalManager()
    session = await manager.create(command=ECHO_CMD)

    assert isinstance(session, TerminalSession)
    assert session.terminal_id
    assert session.terminal_id in manager.sessions
    await manager.close(session.terminal_id)


@pytest.mark.asyncio
async def test_terminal_manager_close_removes_session():
    manager = TerminalManager()
    session = await manager.create(command=ECHO_CMD)
    await manager.close(session.terminal_id)

    assert session.terminal_id not in manager.sessions


@pytest.mark.asyncio
async def test_terminal_manager_close_unknown_id_is_noop():
    manager = TerminalManager()
    await manager.close("nonexistent")  # should not raise


@pytest.mark.asyncio
async def test_terminal_manager_get_returns_session():
    manager = TerminalManager()
    session = await manager.create(command=ECHO_CMD)
    fetched = manager.get(session.terminal_id)
    assert fetched is session
    await manager.close(session.terminal_id)


@pytest.mark.asyncio
async def test_terminal_manager_get_unknown_returns_none():
    manager = TerminalManager()
    assert manager.get("nonexistent") is None


# ── TerminalSession properties ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_session_has_pid():
    manager = TerminalManager()
    session = await manager.create(command=ECHO_CMD)
    assert session.pid > 0
    await manager.close(session.terminal_id)


@pytest.mark.asyncio
async def test_terminal_session_resize_updates_dimensions():
    manager = TerminalManager()
    session = await manager.create(command=ECHO_CMD)
    await session.resize(cols=120, rows=40)
    assert session.cols == 120
    assert session.rows == 40
    await manager.close(session.terminal_id)


# ── TerminalOutput notification ────────────────────────────────────────


def test_terminal_output_serializes_to_notification_params():
    output = TerminalOutput(terminal_id="t1", data="hello world")
    params = output.to_notification_params()
    assert params["terminal_id"] == "t1"
    assert params["data"] == "hello world"


# ── command produces output ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_command_produces_output():
    """A command that prints 'hello' should produce output containing 'hello'."""
    manager = TerminalManager()
    session = await manager.create(command=ECHO_CMD)

    outputs: list[str] = []
    async for chunk in session.read():
        outputs.append(chunk)
        if "hello" in chunk:
            break

    assert any("hello" in o for o in outputs)
    await manager.close(session.terminal_id)


# ── Gateway JSON-RPC integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_terminal_create_method():
    """terminal.start JSON-RPC method spawns a terminal and returns terminal_id."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(
        id=1,
        method="terminal.start",
        params={"command": ECHO_CMD, "cols": 80, "rows": 25},
    )
    result = await session.dispatch_request(request)

    assert result.id == 1
    assert "terminal_id" in result.result
    assert result.result["terminal_id"]
    # Clean up
    await session.terminal_manager.close(result.result["terminal_id"])


@pytest.mark.asyncio
async def test_gateway_terminal_close_method():
    """terminal.stop JSON-RPC method closes a terminal."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    create_req = JsonRpcRequest(
        id=1, method="terminal.start", params={"command": ECHO_CMD},
    )
    create_result = await session.dispatch_request(create_req)
    terminal_id = create_result.result["terminal_id"]

    close_req = JsonRpcRequest(
        id=2, method="terminal.stop", params={"terminal_id": terminal_id},
    )
    close_result = await session.dispatch_request(close_req)

    assert close_result.id == 2
    assert close_result.result["closed"] is True
    assert terminal_id not in session.terminal_manager.sessions


@pytest.mark.asyncio
async def test_gateway_terminal_resize_method():
    """terminal.resize JSON-RPC method adjusts PTY dimensions."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    create_req = JsonRpcRequest(
        id=1, method="terminal.start", params={"command": ECHO_CMD},
    )
    create_result = await session.dispatch_request(create_req)
    terminal_id = create_result.result["terminal_id"]

    resize_req = JsonRpcRequest(
        id=2,
        method="terminal.resize",
        params={"terminal_id": terminal_id, "cols": 120, "rows": 40},
    )
    resize_result = await session.dispatch_request(resize_req)

    assert resize_result.id == 2
    assert resize_result.result["cols"] == 120
    assert resize_result.result["rows"] == 40
    await session.terminal_manager.close(terminal_id)


@pytest.mark.asyncio
async def test_gateway_terminal_input_method():
    """terminal.input JSON-RPC method sends input to the PTY."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    # Use an interactive command (cmd.exe) so we can send input
    cmd = [sys.executable, "-c", "input(); print('got input')"]
    create_req = JsonRpcRequest(
        id=1, method="terminal.start", params={"command": cmd},
    )
    create_result = await session.dispatch_request(create_req)
    terminal_id = create_result.result["terminal_id"]

    input_req = JsonRpcRequest(
        id=2,
        method="terminal.input",
        params={"terminal_id": terminal_id, "data": "test data\n"},
    )
    input_result = await session.dispatch_request(input_req)

    assert input_result.id == 2
    assert input_result.result["written"] is True
    await session.terminal_manager.close(terminal_id)


# ── terminal.output notification ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_terminal_output_notification():
    """When a terminal produces output, a terminal.output notification is sent."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    class FakeClient:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_text(self, text: str) -> None:
            self.messages.append(text)

    client = FakeClient()
    await session.connect(client)  # sends snapshot

    create_req = JsonRpcRequest(
        id=1, method="terminal.start", params={"command": ECHO_CMD},
    )
    create_result = await session.dispatch_request(create_req)
    terminal_id = create_result.result["terminal_id"]

    # Wait for terminal.output notification
    import json
    for _ in range(100):
        for msg in client.messages:
            parsed = json.loads(msg)
            if (
                parsed.get("method") == "terminal.output"
                and parsed["params"]["terminal_id"] == terminal_id
                and "hello" in parsed["params"]["data"]
            ):
                await session.terminal_manager.close(terminal_id)
                return
        await asyncio.sleep(0.1)

    await session.terminal_manager.close(terminal_id)
    pytest.fail("Did not receive terminal.output notification with 'hello'")
