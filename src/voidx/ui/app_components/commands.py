"""Prompt completions for the TUI."""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion


class SlashCommandCompleter(Completer):
    def __init__(self, commands: list[tuple[str, str]]):
        self.commands = commands

    def get_completions(self, document, complete_event):
        text = document.text
        if text.startswith("/"):
            p = text.lower()
            for name, desc in self.commands:
                if name.lower().startswith(p):
                    yield Completion(name, start_position=-len(text), display_meta=desc)
