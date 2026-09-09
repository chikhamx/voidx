import pytest
from voidx.agent.domain.ui_events import (
    IntegrationStartupItem,
    IntegrationStartupUpdated,
    IntegrationStartupFinished,
    TurnStarted,
)
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.consumers import DockEventConsumer


def test_integration_startup_events_schema():
    item1 = IntegrationStartupItem(
        category="mcp",
        key="mcp:tavily",
        label="tavily",
        status="connecting",
    )
    assert item1.category == "mcp"
    assert item1.status == "connecting"

    item2 = IntegrationStartupItem(
        category="lsp",
        key="lsp:python",
        label="python",
        detail="/usr/bin/python3 [PATH]",
        status="warming",
    )
    assert item2.detail == "/usr/bin/python3 [PATH]"
    assert item2.status == "warming"

    event = IntegrationStartupUpdated(items=[item1, item2])
    assert event.kind == "integration_startup.updated"
    assert len(event.items) == 2

    finished = IntegrationStartupFinished()
    assert finished.kind == "integration_startup.finished"


def test_dock_handles_integration_startup_items_without_transcript_nodes():
    dock = BottomInputDock()
    assert not dock.has_active_integration_startup()
    assert dock.active_integration_startup_lines(80) == []

    items = [
        IntegrationStartupItem(
            category="mcp",
            key="mcp:tavily",
            label="tavily",
            status="connecting",
        ),
        IntegrationStartupItem(
            category="lsp",
            key="lsp:python",
            label="python",
            detail="/opt/homebrew/bin/node [CursorPyright (Cursor ext)]",
            status="warming",
        ),
    ]

    dock.set_integration_startup_items(items)
    assert dock.has_active_integration_startup()

    lines = dock.active_integration_startup_lines(100)
    assert len(lines) >= 2
    # Verify MCP line and LSP line exist
    assert any("tavily" in line for line in lines)
    assert any("python" in line and "warming" in line for line in lines)

    # Invariant: No permanent message nodes in tree
    assert len(dock.tree.root.children) == 0

    # Update item to ready
    ready_items = [
        IntegrationStartupItem(
            category="mcp",
            key="mcp:tavily",
            label="tavily",
            status="ready",
        ),
        IntegrationStartupItem(
            category="lsp",
            key="lsp:python",
            label="python",
            detail="/opt/homebrew/bin/node [CursorPyright (Cursor ext)]",
            status="ready",
        ),
    ]
    dock.set_integration_startup_items(ready_items)
    lines = dock.active_integration_startup_lines(100)
    assert any("ready" in line for line in lines)

    # Clear startup items
    dock.clear_integration_startup()
    assert not dock.has_active_integration_startup()
    assert dock.active_integration_startup_lines(100) == []
    assert len(dock.tree.root.children) == 0


def test_dock_event_consumer_integration_startup():
    dock = BottomInputDock()
    consumer = DockEventConsumer(dock)

    item = IntegrationStartupItem(
        category="mcp",
        key="mcp:typex",
        label="typex",
        status="connecting",
    )
    consumer.handle(IntegrationStartupUpdated(items=[item]))
    assert dock.has_active_integration_startup()
    assert any("typex" in line for line in dock.active_integration_startup_lines(80))

    # Finished event clears it
    consumer.handle(IntegrationStartupFinished())
    assert not dock.has_active_integration_startup()
    assert dock.active_integration_startup_lines(80) == []


def test_turn_started_clears_integration_startup():
    dock = BottomInputDock()
    consumer = DockEventConsumer(dock)

    item = IntegrationStartupItem(
        category="mcp",
        key="mcp:typex",
        label="typex",
        status="connecting",
    )
    consumer.handle(IntegrationStartupUpdated(items=[item]))
    assert dock.has_active_integration_startup()

    # Starting a new turn must immediately clear any startup status
    consumer.handle(TurnStarted(text="hello"))
    assert not dock.has_active_integration_startup()


def test_turn_started_permanently_dismisses_startup_updates():
    dock = BottomInputDock()
    consumer = DockEventConsumer(dock)

    item1 = IntegrationStartupItem(
        category="mcp",
        key="mcp:tavily",
        label="tavily",
        status="connecting",
    )
    consumer.handle(IntegrationStartupUpdated(items=[item1]))
    assert dock.has_active_integration_startup()

    # User submits input and turn starts
    consumer.handle(TurnStarted(text="fix bug"))
    assert not dock.has_active_integration_startup()

    # Subsequent delayed update from background startup must NOT revive startup display
    item2 = IntegrationStartupItem(
        category="mcp",
        key="mcp:tavily",
        label="tavily",
        status="ready",
    )
    consumer.handle(IntegrationStartupUpdated(items=[item2]))
    assert not dock.has_active_integration_startup()
    assert dock.active_integration_startup_lines(80) == []


def test_mcp_partial_failure_formatting():
    dock = BottomInputDock()
    items = [
        IntegrationStartupItem(
            category="mcp",
            key="mcp:tavily",
            label="tavily",
            status="ready",
        ),
        IntegrationStartupItem(
            category="mcp",
            key="mcp:bad_server",
            label="bad_server",
            status="failed",
            error="connection refused",
        ),
    ]
    dock.set_integration_startup_items(items)
    lines = dock.active_integration_startup_lines(100)

    # Both ready and failed should be rendered
    assert any("MCP ready:" in line and "tavily" in line for line in lines)
    assert any("MCP failed:" in line and "bad_server" in line for line in lines)
