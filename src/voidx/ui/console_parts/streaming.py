"""Streaming assistant renderer."""

from __future__ import annotations

import time
from types import TracebackType

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from voidx.ui.console_parts.formatting import _next_spin
from voidx.ui.dock import dock
from voidx.ui.events import (
    AssistantStreamStarted,
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamUpdated,
    StatusFinished,
    StatusUpdated,
    ui_events,
)


class StreamingRenderer:
    """Smooth streaming with Rich Live + Markdown rendering."""

    FLUSH_INTERVAL = 0.05

    def __init__(
        self,
        console: Console,
        debug: bool = True,
        stream_to_dock: bool = True,
        agent_id: int = -1,
    ) -> None:
        self._console = console
        self._debug = debug
        self._stream_to_dock = stream_to_dock
        self._agent_id = agent_id
        self._thinking: list[str] = []
        self._thinking_full: list[str] = []
        self._accumulated: str = ""
        self._phase: str = "thinking"
        self._last_flush: float = 0.0
        self._live: Live | None = None
        self._start_time: float = time.monotonic()
        self._first_text: bool = True
        self._discard: bool = False
        stamp = time.time_ns()
        self._thinking_status_id = f"agent:{agent_id}:thinking:{stamp}"
        self._streaming_status_id = f"agent:{agent_id}:streaming:{stamp}"
        self._status_started = False
        self._streaming_status_started = False

    async def __aenter__(self):
        self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc_val: BaseException | None, exc_tb: TracebackType | None,
    ) -> None:
        self.done()

    def start(self) -> None:
        if self._status_started:
            return
        self._status_started = True
        if dock.active and self._stream_to_dock:
            ui_events.emit_nowait(AssistantStreamStarted(agent_id=self._agent_id))
            ui_events.emit_nowait(StatusUpdated(
                agent_id=self._agent_id,
                status_id=self._thinking_status_id,
                label="Thinking",
                detail="",
                stage="thinking",
            ))

    def feed_thinking(self, text: str) -> None:
        self._thinking.append(text)
        self._thinking_full.append(text)
        if dock.active and self._stream_to_dock:
            ui_events.emit_nowait(StatusUpdated(
                agent_id=self._agent_id,
                status_id=self._thinking_status_id,
                label="Thinking",
                detail=self.get_thinking_text(),
                stage="thinking",
            ))

    def feed_text(self, text: str) -> None:
        if not self._status_started:
            self.start()
        if self._thinking and self._phase == "thinking":
            self._flush_thinking()

        self._phase = "text"

        if self._first_text:
            self._first_text = False
            self._start_streaming_status()
            text = "● " + text.lstrip()

        self._accumulated += text

        if dock.active and self._stream_to_dock:
            now = time.monotonic()
            if now - self._last_flush >= self.FLUSH_INTERVAL:
                if not ui_events.emit_nowait(AssistantStreamUpdated(
                    agent_id=self._agent_id,
                    text=self._accumulated,
                )):
                    dock.set_stream(self._accumulated)
                self._last_flush = now
            return

        if self._live is None:
            self._live = Live(
                Markdown(""), console=self._console,
                refresh_per_second=20, transient=False,
            )
            self._live.start()

        now = time.monotonic()
        if now - self._last_flush >= self.FLUSH_INTERVAL:
            self._live.update(Markdown(self._accumulated))
            dock.after_output()
            self._last_flush = now

    def elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def discard(self) -> None:
        """Mark this renderer's output as discarded — don't commit to dock."""
        self._discard = True

    def done(self) -> str:
        if self._thinking and self._phase == "thinking":
            self._flush_thinking()

        self._finish_live_status()

        if self._live:
            if self._accumulated:
                self._live.update(Markdown(self._accumulated))
            self._live.stop()
            self._live = None
        elif dock.active:
            if self._discard:
                if not ui_events.emit_nowait(AssistantStreamDiscarded(agent_id=self._agent_id)):
                    dock.discard_stream()
            else:
                if self._accumulated:
                    if not ui_events.emit_nowait(AssistantStreamUpdated(
                        agent_id=self._agent_id,
                        text=self._accumulated,
                    )):
                        dock.set_stream(self._accumulated)
                if not ui_events.emit_nowait(AssistantStreamCommitted(agent_id=self._agent_id)):
                    dock.commit_stream()

        full = self._accumulated
        if full.startswith("● "):
            full = full[2:]
        self._accumulated = ""
        self._thinking = []
        self._thinking_full = []
        self._first_text = True
        self._status_started = False
        self._streaming_status_started = False
        return full

    def get_thinking_text(self) -> str:
        return "".join(self._thinking_full)

    def get_body_text(self) -> str:
        return self._accumulated

    THINKING_MAX_LINES = 5

    def _flush_thinking(self) -> None:
        thinking_text = self.get_thinking_text()
        if thinking_text.strip():
            if dock.active:
                if not ui_events.emit_nowait(StatusUpdated(
                    agent_id=self._agent_id,
                    status_id=self._thinking_status_id,
                    label="Thinking",
                    detail=thinking_text,
                    stage="thinking",
                )):
                    node = dock.set_status(self._thinking_status_id, "Thinking", thinking_text, stage="thinking")
                    node.collapsed = False
                self._thinking = []
                return
            lines = thinking_text.split("\n")
            total = len(lines)
            def render(console: Console) -> None:
                if total > self.THINKING_MAX_LINES:
                    skipped = total - self.THINKING_MAX_LINES
                    visible = "\n".join(lines[-self.THINKING_MAX_LINES:])
                    console.print(f"  {_next_spin()} [dim]Thinking… [/dim]", end="")
                    console.print(f"[dim][{skipped} earlier lines folded][/dim]")
                else:
                    visible = thinking_text
                    console.print(f"  {_next_spin()} [dim]Thinking... [/dim]", end="")
                console.print(Text(visible, style="dim italic"))

            if not dock.capture(render):
                render(self._console)
        self._thinking = []

    def _start_streaming_status(self) -> None:
        if self._streaming_status_started or not dock.active or not self._stream_to_dock:
            return
        self._streaming_status_started = True
        self._finish_thinking_status()
        ui_events.emit_nowait(StatusUpdated(
            agent_id=self._agent_id,
            status_id=self._streaming_status_id,
            label="Streaming",
            detail="",
            stage="streaming",
        ))

    def _finish_live_status(self) -> None:
        if not dock.active or not self._stream_to_dock or not self._status_started:
            return
        if self._streaming_status_started:
            ui_events.emit_nowait(StatusFinished(
                agent_id=self._agent_id,
                status_id=self._streaming_status_id,
            ))
        else:
            self._finish_thinking_status()

    def _finish_thinking_status(self) -> None:
        thinking_text = self.get_thinking_text()
        if thinking_text.strip():
            ui_events.emit_nowait(StatusFinished(
                agent_id=self._agent_id,
                status_id=self._thinking_status_id,
                label=f"Thinking for {self.elapsed():.0f}s",
                detail=thinking_text,
                remove=False,
            ))
            return
        ui_events.emit_nowait(StatusFinished(
            agent_id=self._agent_id,
            status_id=self._thinking_status_id,
        ))
