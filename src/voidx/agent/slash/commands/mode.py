"""Slash interaction-mode helpers and /goal command."""
from __future__ import annotations

from voidx.diffing import git_diff, git_diff_stat
from voidx.runtime.intent import InteractionMode
from voidx.runtime.ui import get_dock, paste_clipboard_image, session_tracker, ui


class ModeCommandsMixin:
    def _set_interaction_mode(self, mode: str) -> None:
        from voidx.agent.application.runtime_context import InteractionMode

        parsed = InteractionMode.parse(mode)
        self.host.set_interaction_mode(parsed)
        labels = {
            InteractionMode.AUTO: "Auto",
            InteractionMode.PLAN: "Plan",
            InteractionMode.GOAL: "Goal",
        }
        notes = {
            InteractionMode.PLAN: "write/insert/replace/edit/bash blocked",
            InteractionMode.GOAL: "keep work scoped to the current goal",
        }
        suffix = f" — {notes[parsed]}" if parsed in notes else ""
        ui.print(f"[dim]Mode set to [cyan]{labels[parsed]}[/cyan]{suffix}[/dim]")

    async def _goal(self, arg: str) -> None:
        from voidx.agent.application.runtime_context import InteractionMode
        from voidx.runtime.task_state import TaskState, goal_label

        task_state = self.host.task_state or TaskState()
        goal = arg.strip()

        if not goal:
            if task_state.current_goal is not None:
                ui.print(f"Goal: [cyan]{goal_label(task_state.current_goal)}[/cyan]")
            else:
                ui.print("Usage: /goal <goal>")
            return

        task_state.set_goal(goal)
        self.host.set_task_state(task_state)
        self._set_interaction_mode(InteractionMode.GOAL.value)
        await self.host.persist_runtime_state()
        await self.host.set_session_title(task_state.current_goal.desc)
        ui.print(f"[dim]Goal set to [cyan]{goal_label(task_state.current_goal)}[/cyan][/dim]")

    def _usage(self) -> None:
        from voidx.llm.usage import format_cache_hit_rate, format_token_count

        stats = self.host.usage_stats
        if stats is None:
            ui.print("[dim]No usage data available.[/dim]")
            return

        ui.print("[bold]Token Usage[/bold]")
        ui.print(
            f"  Context: [cyan]{format_token_count(stats.context_tokens)}[/cyan]"
            f" / {format_token_count(stats.context_limit)}"
        )
        ui.print(
            f"  Last call: in [cyan]{format_token_count(stats.last_input_tokens)}[/cyan]"
            f" · out [cyan]{format_token_count(stats.last_output_tokens)}[/cyan]"
            " · cache read "
            f"[cyan]{format_token_count(stats.last_cache_read_tokens or stats.last_estimated_cache_read_tokens)}[/cyan]"
            f" · write [cyan]{format_token_count(stats.last_cache_write_tokens)}[/cyan]"
        )
        ui.print(
            f"  Session: in [cyan]{format_token_count(stats.total_input_tokens)}[/cyan]"
            f" · out [cyan]{format_token_count(stats.total_output_tokens)}[/cyan]"
            f" · total [cyan]{format_token_count(stats.total_tokens)}[/cyan]"
            f" · cache {format_cache_hit_rate(stats)}"
            f" · calls {stats.total_calls}"
        )

    def _debug(self, arg: str) -> None:
        value = arg.strip().lower()
        if value in ("on", "true", "1", "yes"):
            self.host.set_debug(True)
        elif value in ("off", "false", "0", "no"):
            self.host.set_debug(False)
        elif value:
            ui.error("Usage: /debug [on|off]")
            return
        else:
            self.host.set_debug(not self.host.debug_enabled())

        state = "on" if self.host.debug_enabled() else "off"
        ui.print(f"[dim]debug {state}[/dim]")

    def _log(self, arg: str) -> None:
        config = self.host.config
        if config is None:
            ui.error("No config available.")
            return

        parts = arg.strip().split()
        if not parts:
            ex = "on" if config.log_llm_exchange else "off"
            di = "on" if config.log_llm_diagnostic else "off"
            ui.print(f"[dim]log exchange {ex}, diagnostic {di}[/dim]")
            return

        target = parts[0].lower()
        if target not in ("exchange", "diagnostic"):
            ui.error("Usage: /log [exchange|diagnostic] [on|off]")
            return

        if len(parts) < 2:
            if target == "exchange":
                config.log_llm_exchange = not config.log_llm_exchange
            else:
                config.log_llm_diagnostic = not config.log_llm_diagnostic
        else:
            value = parts[1].lower()
            if value in ("on", "true", "1", "yes"):
                flag = True
            elif value in ("off", "false", "0", "no"):
                flag = False
            else:
                ui.error("Usage: /log [exchange|diagnostic] [on|off]")
                return
            if target == "exchange":
                config.log_llm_exchange = flag
            else:
                config.log_llm_diagnostic = flag

        ex = "on" if config.log_llm_exchange else "off"
        di = "on" if config.log_llm_diagnostic else "off"
        ui.print(f"[dim]log exchange {ex}, diagnostic {di}[/dim]")

    def _parallel(self, arg: str) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings available.")
            return

        value = arg.strip().lower()
        active = self.host.config.parallel_subagents
        saved = settings.get_parallel_subagents()

        if value in ("on", "true", "1", "yes"):
            enabled = True
        elif value in ("off", "false", "0", "no"):
            enabled = False
        elif value == "status":
            self._print_parallel_status(active, saved)
            return
        elif value:
            ui.error("Usage: /parallel [on|off|status]")
            return
        else:
            enabled = not saved.enabled

        saved = saved.model_copy(update={"enabled": enabled})
        settings.set_parallel_subagents(saved)
        state = "on" if saved.enabled else "off"
        ui.print(
            f"[dim]Saved parallel subagents {state} "
            f"(max_concurrent={saved.max_concurrent}). "
            "Run /clear or restart to apply.[/dim]"
        )

    def _print_parallel_status(self, active, saved) -> None:
        saved_state = "on" if saved.enabled else "off"
        active_state = "on" if active.enabled else "off"
        if active.enabled == saved.enabled and active.max_concurrent == saved.max_concurrent:
            ui.print(
                f"[dim]parallel subagents {active_state} "
                f"(max_concurrent={active.max_concurrent})[/dim]"
            )
            return

        ui.print(
            f"[dim]parallel subagents current {active_state} "
            f"(max_concurrent={active.max_concurrent}); saved {saved_state} "
            f"(max_concurrent={saved.max_concurrent}). "
            "Run /clear or restart to apply.[/dim]"
        )

    def _paste_clipboard_image(self) -> None:
        result = paste_clipboard_image(self.host.workspace)
        if result.ok:
            ui.print(f"[dim]{result.message}[/dim]")
            return
        ui.error(result.message)

    async def _show_diff(self) -> None:
        workspace = self.host.workspace
        stat = git_diff_stat(workspace)
        if stat:
            ui.print(f"[bold]Changes:[/bold]\n{stat}\n")
            diff_text = git_diff(workspace)
            if diff_text:
                ui.diff(diff_text)
            else:
                ui.print("[dim]No diff content.[/dim]")
        else:
            ui.print("[dim]No changes in working tree.[/dim]")

    async def _clear(self) -> None:
        await self.host.clear_current_session()
        session_tracker.clear()
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._show_startup(prefer_direct=True)

