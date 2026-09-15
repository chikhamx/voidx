"""Real-PTY acceptance for Ghostty height-shrink viewport boundary.

A real PureTui child process runs in a forked pty (TERM_PROGRAM=ghostty),
drives committed history plus an active subagent + Wait through the full
worker-writer / synchronized-output path, and is resized via TIOCSWINSZ with a
Ghostty-modeled screen rearrangement. Assertions mirror
test_resize_history_boundary.py.
"""

import asyncio

import pytest

from pty_ghostty_harness import EXIT_KEY, PtySession, _ghostty_shrink

HISTORY_MARKERS = tuple(f"PTY-HISTORY-{i:02}" for i in range(35))


async def _build_scenario(session: PtySession) -> None:
    await session.settle()
    session.send_key("H")      # build + commit 35 history nodes
    await session.settle()
    session.send_key("I")      # active subagent + Wait
    await session.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("heights", [(20,), (16,), (11,), (20, 16, 11)])
async def test_pty_ghostty_shrink_preserves_history_and_dynamic_boundary(
    tmp_path, heights
):
    with PtySession(tmp_path) as session:
        await _build_scenario(session)
        screen = session.screen

        # Precondition: full-load, committed history visible, dynamic region live.
        assert any("PTY-ACTIVE-AGENT" in row for row in screen.rows)
        assert any('Wait("Prism")' in row for row in screen.rows)
        assert len(screen.history) > 0

        resized_history = screen.history
        for height in heights:
            resized_history = _ghostty_shrink(screen, height)
            session.send_resize(height)
            await session.settle()

            assert not session.errors
            # Dynamic region appears exactly once in the visible area.
            assert sum(
                "PTY-ACTIVE-AGENT" in row for row in screen.rows
            ) == 1, screen.rows
            assert sum('Wait("Prism")' in row for row in screen.rows) == 1
            # The app's repaint must not push anything new into history (B→C).
            assert screen.history == resized_history
            # Committed history order is preserved across history + viewport.
            assert tuple(
                row
                for row in (*screen.history, *screen.rows)
                if row.startswith("PTY-HISTORY-")
            ) == HISTORY_MARKERS

        # A no-op query must not move history again.
        session.send_key(EXIT_KEY)
        await asyncio.sleep(0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize("heights", [(20,), (16,)])
async def test_pty_ghostty_shrink_after_subagent_finished(tmp_path, heights):
    """Real-PTY: subagent + Wait complete (commit clears frame lines), then
    shrink. The frame-geometry fallback must keep the boundary correct."""
    with PtySession(tmp_path) as session:
        await _build_scenario(session)
        screen = session.screen
        assert any("PTY-ACTIVE-AGENT" in row for row in screen.rows)

        # Subagent + Wait complete; their nodes settle into committed history.
        session.send_key("F")
        await session.settle()

        resized_history = screen.history
        for height in heights:
            resized_history = _ghostty_shrink(screen, height)
            session.send_resize(height)
            await session.settle()

            assert not session.errors
            # Settled nodes appear at most once; no duplicate active copies.
            assert sum("PTY-ACTIVE-AGENT" in row for row in screen.rows) <= 1
            assert screen.history == resized_history
            assert tuple(
                row
                for row in (*screen.history, *screen.rows)
                if row.startswith("PTY-HISTORY-")
            ) == HISTORY_MARKERS

        session.send_key(EXIT_KEY)
        await asyncio.sleep(0.2)
