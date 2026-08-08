"""Streaming assistant renderer."""

from __future__ import annotations

import time
from types import TracebackType

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from voidx.presentation.output.console.formatting import _next_spin
from voidx.presentation.output.dock import state as dock_state
from voidx.presentation.output.events import (
    AssistantStreamStarted,
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamUpdated,
    ui_events,
)


class StreamingRenderer:
    """Smooth streaming with Rich Live + Markdown rendering."""

    FLUSH_INTERVAL = 0.1

    def __init__(
        self,
        console: Console,
        debug: bool = True,
        stream_to_dock: bool = True,
        agent_id: int = -1,
        headless: bool = False,
    ) -> None:
        self._console = console
        self._debug = debug
        self._stream_to_dock = stream_to_dock
        self._agent_id = agent_id
        self._headless = headless
        self._thinking: list[str] = []
        self._thinking_full: list[str] = []
        self._accumulated: str = ""
        self._phase: str = "thinking"
        self._last_flush: float = 0.0
        self._live: Live | None = None
        self._start_time: float = time.monotonic()
        self._first_text: bool = True
        self._discard: bool = False
        self._stream_started = False

    async def __aenter__(self):
        self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc_val: BaseException | None, exc_tb: TracebackType | None,
    ) -> None:
        self.done()

    def start(self) -> None:
        if self._stream_started:
            return
        self._stream_started = True
        if self._headless:
            return
        if dock_state.dock.active and self._stream_to_dock:
            ui_events.emitnowait(AssistantStreamStarted(agent_id=self._agent_id))

    def feed_thinking(self, text: str) -> None:
        if not self._stream_started:
            self.start()
        self._thinking.append(text)
        self._thinking_full.append(text)
        if self._headless:
            return
        if dock_state.dock.active and self._stream_to_dock and self._phase == "thinking":
            if not ui_events.emitnowait(AssistantStreamUpdated(
                agent_id=self._agent_id,
                text=self.get_thinking_text(),
                phase="thinking",
            )):
                dock_state.dock.set_stream(self.get_thinking_text(), phase="thinking")

    def feed_text(self, text: str) -> None:
        if not self._stream_started:
            self.start()
        if self._thinking and self._phase == "thinking":
            self._flush_thinking()

        self._phase = "text"

        if self._first_text:
            self._first_text = False
            text = "● " + text.lstrip()

        self._accumulated += text
        if self._headless:
            return

        if dock_state.dock.active and self._stream_to_dock:
            now = time.monotonic()
            if now - self._last_flush >= self.FLUSH_INTERVAL:
                if not ui_events.emitnowait(AssistantStreamUpdated(
                    agent_id=self._agent_id,
                    text=self._accumulated,
                    phase="text",
                )):
                    dock_state.dock.set_stream(self._accumulated, phase="text")
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
            dock_state.dock.after_output()
            self._last_flush = now

    def elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def discard(self) -> None:
        """Mark this renderer's output as discarded — don't commit to dock_state.dock."""
        self._discard = True

    def done(self) -> str:
        if self._thinking and self._phase == "thinking":
            self._flush_thinking()

        if self._live:
            if self._accumulated:
                self._live.update(Markdown(self._accumulated))
            self._live.stop()
            self._live = None
        elif dock_state.dock.active and self._stream_to_dock and not self._headless:
            if self._discard:
                if not ui_events.emitnowait(AssistantStreamDiscarded(agent_id=self._agent_id)):
                    dock_state.dock.discard_stream()
            else:
                if self._accumulated:
                    if not ui_events.emitnowait(AssistantStreamUpdated(
                        agent_id=self._agent_id,
                        text=self._accumulated,
                        phase="text",
                    )):
                        dock_state.dock.set_stream(
                            self._accumulated,
                            phase="text",
                            refresh=False,
                        )
                    if not ui_events.emitnowait(AssistantStreamCommitted(agent_id=self._agent_id)):
                        dock_state.dock.commit_stream()
                elif self._thinking_full:
                    thinking_text = self.get_thinking_text()
                    if not ui_events.emitnowait(AssistantStreamUpdated(
                        agent_id=self._agent_id,
                        text=thinking_text,
                        phase="thinking",
                    )):
                        dock_state.dock.set_stream(thinking_text, phase="thinking", refresh=False)
                    if not ui_events.emitnowait(AssistantStreamCommitted(agent_id=self._agent_id)):
                        dock_state.dock.commit_stream()
                elif self._stream_started:
                    if not ui_events.emitnowait(AssistantStreamDiscarded(agent_id=self._agent_id)):
                        dock_state.dock.discard_stream()

        full = self._accumulated
        if full.startswith("● "):
            full = full[2:]
        self._accumulated = ""
        self._thinking = []
        self._thinking_full = []
        self._first_text = True
        self._stream_started = False
        self._phase = "thinking"
        return full

    def get_thinking_text(self) -> str:
        return "".join(self._thinking_full)

    def get_body_text(self) -> str:
        return self._accumulated

    THINKING_MAX_LINES = 5

    def _flush_thinking(self) -> None:
        thinking_text = self.get_thinking_text()
        if self._headless:
            self._thinking = []
            return
        if thinking_text.strip():
            if dock_state.dock.active and self._stream_to_dock:
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

            if not dock_state.dock.capture(render):
                render(self._console)
        self._thinking = []
