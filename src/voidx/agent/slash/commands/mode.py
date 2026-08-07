"""Slash interaction-mode helpers and /goal command."""
from __future__ import annotations

from voidx.tooling.adapters.git_diff import git_diff, git_diff_stat
from voidx.agent.domain.task.intent import InteractionMode
from voidx.runtime.ui import get_dock, paste_clipboard_image, session_tracker, ui


class ModeCommandsMixin:
    def _set_interaction_mode(self, mode: str) -> None:
        from voidx.agent.application.runtime_context import InteractionMode

        parsed = InteractionMode.parse(mode)
        self.host.set_interaction_mode(parsed)
        labels = {
            InteractionMode.AUTO: "Auto",
            InteractionMode.PLAN: "Plan",
        }
        notes = {
            InteractionMode.PLAN: "write/insert/replace/edit/bash blocked",
        }
        suffix = f" — {notes[parsed]}" if parsed in notes else ""
        ui.print(f"[dim]Mode set to [cyan]{labels.get(parsed, parsed.value)}[/cyan]{suffix}[/dim]")

    async def _goal(self, arg: str) -> None:
        from voidx.agent.domain.automation.goal import GoalSpec

        text = arg.strip()
        if not text:
            await self._switch_profile("goal")
            return
        service = getattr(self.host, "goal_service", None)
        if service is None:
            ui.print("[dim]/goal runtime is not available in this session.[/dim]")
            return
        parent_thread_id = getattr(getattr(self.host, "session", None), "id", None)
        if text == "status":
            status = await service.status(parent_thread_id)
            if status is None:
                ui.print("[dim]/goal is not active.[/dim]")
                return
            ui.print(
                f"[dim]/goal active: [cyan]{status.objective_summary}[/cyan] "
                f"attempt {status.attempt_count}/{status.max_attempts} state={status.state}[/dim]"
            )
            return
        if text == "stop":
            stopped = await service.stop(parent_thread_id)
            ui.print("[dim]/goal stopped.[/dim]" if stopped else "[dim]/goal is not active.[/dim]")
            return
        objective, acceptance = _parse_goal_args(text)
        if not objective or not acceptance:
            ui.print("Usage: /goal <objective> --accept <acceptance condition>")
            return
        status = await service.start(
            parent_thread_id,
            GoalSpec(objective=objective, acceptance_condition=acceptance),
        )
        ui.print(
            f"[dim]/goal started: [cyan]{status.objective_summary}[/cyan] "
            f"attempt {status.attempt_count}/{status.max_attempts}[/dim]"
        )

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



def _parse_goal_args(text: str) -> tuple[str, str]:
    marker = " --accept "
    if marker not in text:
        return text.strip(), ""
    objective, acceptance = text.split(marker, 1)
    return objective.strip(), acceptance.strip()
