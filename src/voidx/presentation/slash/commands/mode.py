"""Slash interaction-mode helpers and /goal command."""
from __future__ import annotations

from voidx.tooling.adapters.git_diff import git_diff, git_diff_stat
from voidx.agent.domain.task.intent import InteractionMode
from voidx.presentation.slash.helpers import (
    _format_cache_hit_rate,
    _format_token_count,
)


class ModeCommandsMixin:
    def _set_interaction_mode(self, mode: str) -> None:
        from voidx.agent.domain.task.intent import InteractionMode

        parsed = InteractionMode.parse(mode)
        self.mode_port.set_interaction_mode(parsed)
        labels = {
            InteractionMode.AUTO: "Auto",
            InteractionMode.PLAN: "Plan",
        }
        notes = {
            InteractionMode.PLAN: "write/insert/replace/edit/bash blocked",
        }
        suffix = f" — {notes[parsed]}" if parsed in notes else ""
        self.mode_port.ui.print(f"[dim]Mode set to [cyan]{labels.get(parsed, parsed.value)}[/cyan]{suffix}[/dim]")

    async def _goal(self, arg: str) -> None:
        from voidx.agent.domain.automation.goal import GoalSpec

        text = arg.strip()
        if not text:
            await self._switch_profile("goal")
            return
        service = self.mode_port.goal_service
        if service is None:
            self.mode_port.ui.print("[dim]/goal runtime is not available in this session.[/dim]")
            return
        parent_thread_id = self.mode_port.session.id if self.mode_port.session is not None else None
        if text == "status":
            status = await service.status(parent_thread_id)
            if status is None:
                self.mode_port.ui.print("[dim]/goal is not active.[/dim]")
                return
            self.mode_port.ui.print(
                f"[dim]/goal active: [cyan]{status.objective_summary}[/cyan] "
                f"attempt {status.attempt_count}/{status.max_attempts} state={status.state}[/dim]"
            )
            return
        if text == "stop":
            stopped = await service.stop(parent_thread_id)
            self.mode_port.ui.print("[dim]/goal stopped.[/dim]" if stopped else "[dim]/goal is not active.[/dim]")
            return
        objective, acceptance = _parse_goal_args(text)
        if not objective or not acceptance:
            self.mode_port.ui.print("Usage: /goal <objective> --accept <acceptance condition>")
            return
        status = await service.start(
            parent_thread_id,
            GoalSpec(objective=objective, acceptance_condition=acceptance),
        )
        self.mode_port.ui.print(
            f"[dim]/goal started: [cyan]{status.objective_summary}[/cyan] "
            f"attempt {status.attempt_count}/{status.max_attempts}[/dim]"
        )

    def _usage(self) -> None:

        stats = self.mode_port.usage_stats
        if stats is None:
            self.mode_port.ui.print("[dim]No usage data available.[/dim]")
            return

        self.mode_port.ui.print("[bold]Token Usage[/bold]")
        self.mode_port.ui.print(
            f"  Context: [cyan]{_format_token_count(stats.context_tokens)}[/cyan]"
            f" / {_format_token_count(stats.context_limit)}"
        )
        self.mode_port.ui.print(
            f"  Last call: in [cyan]{_format_token_count(stats.last_input_tokens)}[/cyan]"
            f" · out [cyan]{_format_token_count(stats.last_output_tokens)}[/cyan]"
            " · cache read "
            f"[cyan]{_format_token_count(stats.last_cache_read_tokens or stats.last_estimated_cache_read_tokens)}[/cyan]"
            f" · write [cyan]{_format_token_count(stats.last_cache_write_tokens)}[/cyan]"
        )
        self.mode_port.ui.print(
            f"  Session: in [cyan]{_format_token_count(stats.total_input_tokens)}[/cyan]"
            f" · out [cyan]{_format_token_count(stats.total_output_tokens)}[/cyan]"
            f" · total [cyan]{_format_token_count(stats.total_tokens)}[/cyan]"
            f" · cache {_format_cache_hit_rate(stats)}"
            f" · calls {stats.total_calls}"
        )

    def _debug(self, arg: str) -> None:
        value = arg.strip().lower()
        if value in ("on", "true", "1", "yes"):
            self.mode_port.set_debug(True)
        elif value in ("off", "false", "0", "no"):
            self.mode_port.set_debug(False)
        elif value:
            self.mode_port.ui.error("Usage: /debug [on|off]")
            return
        else:
            self.mode_port.set_debug(not self.mode_port.debug_enabled())

        state = "on" if self.mode_port.debug_enabled() else "off"
        self.mode_port.ui.print(f"[dim]debug {state}[/dim]")

    def _log(self, arg: str) -> None:
        config = self.mode_port.log_config
        if config is None:
            self.mode_port.ui.error("No config available.")
            return

        parts = arg.strip().split()
        if not parts:
            ex = "on" if config.log_llm_exchange else "off"
            di = "on" if config.log_llm_diagnostic else "off"
            self.mode_port.ui.print(f"[dim]log exchange {ex}, diagnostic {di}[/dim]")
            return

        target = parts[0].lower()
        if target not in ("exchange", "diagnostic"):
            self.mode_port.ui.error("Usage: /log [exchange|diagnostic] [on|off]")
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
                self.mode_port.ui.error("Usage: /log [exchange|diagnostic] [on|off]")
                return
            if target == "exchange":
                config.log_llm_exchange = flag
            else:
                config.log_llm_diagnostic = flag

        ex = "on" if config.log_llm_exchange else "off"
        di = "on" if config.log_llm_diagnostic else "off"
        self.mode_port.ui.print(f"[dim]log exchange {ex}, diagnostic {di}[/dim]")


    def _paste_clipboard_image(self) -> None:
        result = self.mode_port.clipboard_image.paste_clipboard_image(self.mode_port.workspace)
        if result.ok:
            self.mode_port.ui.print(f"[dim]{result.message}[/dim]")
            return
        self.mode_port.ui.error(result.message)

    async def _show_diff(self) -> None:
        workspace = self.mode_port.workspace
        stat = git_diff_stat(workspace)
        if stat:
            self.mode_port.ui.print(f"[bold]Changes:[/bold]\n{stat}\n")
            diff_text = git_diff(workspace)
            if diff_text:
                self.mode_port.ui.diff(diff_text)
            else:
                self.mode_port.ui.print("[dim]No diff content.[/dim]")
        else:
            self.mode_port.ui.print("[dim]No changes in working tree.[/dim]")

    async def _clear(self) -> None:
        await self.mode_port.clear_current_session()
        self.mode_port.ui_state.session_tracker.clear()
        active_dock = self.mode_port.ui_state.get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._show_startup(prefer_direct=True)



def _parse_goal_args(text: str) -> tuple[str, str]:
    marker = " --accept "
    if marker not in text:
        return text.strip(), ""
    objective, acceptance = text.split(marker, 1)
    return objective.strip(), acceptance.strip()
