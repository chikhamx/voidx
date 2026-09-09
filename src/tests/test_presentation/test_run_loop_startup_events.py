import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from voidx.agent.domain.ui_events import (
    IntegrationStartupUpdated,
    IntegrationStartupFinished,
)
from voidx.presentation.terminal.run_loop import TerminalRunLoop


@pytest.mark.asyncio
async def test_run_loop_emits_startup_events_and_no_transcript_messages():
    emitted_events = []

    class FakeEvents:
        async def emit(self, event):
            emitted_events.append(event)
            return True

    mock_dock = MagicMock()
    mock_ui = MagicMock()
    mock_ui.events = FakeEvents()
    mock_ui.dock = mock_dock

    mock_integrations = MagicMock()
    mock_integrations.has_mcp.return_value = True
    mock_integrations.enabled_mcp_names.return_value = ("tavily", "typex")
    mock_integrations.start_mcp = AsyncMock()
    mock_integrations.mcp_statuses.return_value = [
        SimpleNamespace(name="tavily", status="connected", error_message=""),
        SimpleNamespace(name="typex", status="connected", error_message=""),
    ]

    mock_integrations.has_lsp.return_value = True
    mock_integrations.initialize_lsp = AsyncMock(return_value=[
        SimpleNamespace(language="python", available=True, enabled=True, detected_source="PATH", resolved_path="/usr/bin/python3"),
    ])
    mock_integrations.warm_up_lsp = AsyncMock(return_value={"python": "ok"})

    run_loop = TerminalRunLoop(
        status_reader=MagicMock(),
        sessions=MagicMock(),
        integrations=mock_integrations,
        frontend_binding=MagicMock(),
        input_port=MagicMock(),
        guidance=MagicMock(),
        workspace_write_lock=MagicMock(),
        ui=mock_ui,
    )

    await run_loop._show_integrations_startup(finish_delay=0.01)

    # Invariant: No append_message calls made to dock!
    assert mock_dock.append_message.call_count == 0

    # Assert events emitted
    updates = [e for e in emitted_events if isinstance(e, IntegrationStartupUpdated)]
    finished = [e for e in emitted_events if isinstance(e, IntegrationStartupFinished)]

    assert len(updates) >= 2
    assert len(finished) == 1

    # Final update items should show ready status
    final_items = {item.key: item for item in updates[-1].items}
    assert final_items["mcp:tavily"].status == "ready"
    assert final_items["mcp:typex"].status == "ready"
    assert final_items["lsp:python"].status == "ready"


@pytest.mark.asyncio
async def test_run_loop_startup_cancels_mcp_task_on_cancellation():
    mcp_started = asyncio.Event()
    mcp_cancelled = asyncio.Event()

    async def slow_start_mcp():
        mcp_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            mcp_cancelled.set()
            raise

    mock_integrations = MagicMock()
    mock_integrations.has_mcp.return_value = True
    mock_integrations.enabled_mcp_names.return_value = ("slow_mcp",)
    mock_integrations.start_mcp = slow_start_mcp
    mock_integrations.has_lsp.return_value = True

    async def slow_lsp():
        await asyncio.sleep(10)
        return []

    mock_integrations.initialize_lsp = slow_lsp

    fake_events = MagicMock()
    fake_events.emit = AsyncMock(return_value=True)
    mock_ui = MagicMock()
    mock_ui.events = fake_events

    run_loop = TerminalRunLoop(
        status_reader=MagicMock(),
        sessions=MagicMock(),
        integrations=mock_integrations,
        frontend_binding=MagicMock(),
        input_port=MagicMock(),
        guidance=MagicMock(),
        workspace_write_lock=MagicMock(),
        ui=mock_ui,
    )

    task = asyncio.create_task(run_loop._show_integrations_startup(finish_delay=0.01))
    await mcp_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Assert mcp_task was also cancelled and did not orphan
    assert mcp_cancelled.is_set()


@pytest.mark.asyncio
async def test_run_loop_startup_handles_lsp_exception_and_cancels_mcp():
    mcp_cancelled = asyncio.Event()

    async def slow_start_mcp():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            mcp_cancelled.set()
            raise

    mock_integrations = MagicMock()
    mock_integrations.has_mcp.return_value = True
    mock_integrations.enabled_mcp_names.return_value = ("slow_mcp",)
    mock_integrations.start_mcp = slow_start_mcp
    mock_integrations.has_lsp.return_value = True

    async def failing_lsp():
        await asyncio.sleep(0.01)
        raise RuntimeError("LSP crash")

    mock_integrations.initialize_lsp = failing_lsp

    fake_events = MagicMock()
    fake_events.emit = AsyncMock(return_value=True)
    mock_ui = MagicMock()
    mock_ui.events = fake_events

    run_loop = TerminalRunLoop(
        status_reader=MagicMock(),
        sessions=MagicMock(),
        integrations=mock_integrations,
        frontend_binding=MagicMock(),
        input_port=MagicMock(),
        guidance=MagicMock(),
        workspace_write_lock=MagicMock(),
        ui=mock_ui,
    )

    # Should not raise exception out of _show_integrations_startup
    await run_loop._show_integrations_startup(finish_delay=0.01)
    assert mcp_cancelled.is_set()
