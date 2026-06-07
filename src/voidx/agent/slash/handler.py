"""Slash command handler — extracted from graph.py to keep it focused."""

from __future__ import annotations

from inspect import isawaitable
from typing import Any, Protocol

from voidx.diffing import git_diff, git_diff_stat
from voidx.agent.slash.code_ide import SlashCodeIdeMixin
from voidx.agent.slash.guide import SlashGuideMixin
from voidx.agent.slash.init import SlashInitMixin
from voidx.agent.slash.lsp import SlashLspMixin
from voidx.agent.slash.mcp import SlashMcpMixin
from voidx.agent.slash.model import SlashModelMixin
from voidx.agent.slash.profile import SlashProfileMixin
from voidx.agent.slash.session import SlashSessionMixin
from voidx.agent.slash.skills import SlashSkillsMixin
from voidx.agent.slash.runtime import PROVIDERS, _select_from_list, _w, prompt_text, ui
from voidx.runtime.ui import COMMANDS


class SlashCommandHost(Protocol):
    config: Any
    api_key: str | None
    model: Any | None

    @property
    def app(self) -> Any | None: ...
    @property
    def permission(self) -> Any: ...
    @property
    def session(self) -> Any | None: ...
    @property
    def settings(self) -> Any | None: ...
    @property
    def task_run(self) -> Any: ...
    @property
    def task_state(self) -> Any: ...
    @property
    def usage_stats(self) -> Any: ...
    @property
    def workspace(self) -> str: ...

    def set_debug(self, value: bool) -> None: ...
    def set_interaction_mode(self, mode: Any) -> Any: ...
    def debug_enabled(self) -> bool: ...
    def set_task_run(self, task_run: Any) -> None: ...
    async def clear_current_session(self) -> None: ...
    async def compact_session_history(self, *, force: bool = True) -> bool: ...
    async def persist_runtime_state(self) -> None: ...
    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool: ...
    async def regenerate_session_title(self) -> bool: ...
    async def resume_session(self, session: Any) -> None: ...
    async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> None: ...
    async def set_session_title(self, title: str) -> None: ...
    async def show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None: ...


class SlashHandler(
    SlashCodeIdeMixin,
    SlashGuideMixin,
    SlashInitMixin,
    SlashLspMixin,
    SlashSessionMixin,
    SlashSkillsMixin,
    SlashMcpMixin,
    SlashProfileMixin,
    SlashModelMixin,
):
    """Handles all slash commands (/help, /model, /plan, etc.).

    Takes a reference to the parent VoidXGraph since commands need access
    to session, config, permission, and model state.
    """

    def __init__(self, graph: SlashCommandHost) -> None:
        self._g = graph

    def _legacy_attr(self, name: str, default: Any = None) -> Any:
        return getattr(self._g, name, default)

    def _set_legacy_attr(self, name: str, value: Any) -> None:
        setattr(self._g, name, value)

    def _host_value(self, public_name: str, legacy_name: str, default: Any = None) -> Any:
        if hasattr(self._g, public_name):
            return getattr(self._g, public_name)
        return self._legacy_attr(legacy_name, default)

    def _host_app(self) -> Any | None:
        return self._host_value("app", "_app")

    def _host_permission(self) -> Any:
        return self._host_value("permission", "_permission")

    def _host_session(self) -> Any | None:
        return self._host_value("session", "_session")

    def _host_settings(self) -> Any | None:
        return self._host_value("settings", "_settings")

    async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
        return await prompt_text(self._host_app(), text, default=default, secret=secret)

    def _host_task_run(self) -> Any:
        return self._host_value("task_run", "_task_run")

    def _set_host_task_run(self, task_run: Any) -> None:
        setter = getattr(self._g, "set_task_run", None)
        if callable(setter):
            setter(task_run)
        else:
            self._set_legacy_attr("_task_run", task_run)

    def _host_task_state(self) -> Any:
        return self._host_value("task_state", "_task_state")

    def _host_usage_stats(self) -> Any:
        return self._host_value("usage_stats", "_usage_stats")

    def _host_workspace(self) -> str:
        return self._host_value("workspace", "_workspace", ".")

    def _host_debug_enabled(self) -> bool:
        getter = getattr(self._g, "debug_enabled", None)
        if callable(getter):
            return bool(getter())
        return bool(self._legacy_attr("_debug", False))

    def _host_interaction_mode_value(self) -> str:
        getter = getattr(self._g, "interaction_mode", None)
        if callable(getter):
            mode = getter()
        else:
            mode = self._legacy_attr("_interaction_mode")
        current = getattr(mode, "value", None)
        if current is None:
            current = "plan" if self._legacy_attr("_plan_mode", False) else "auto"
        return current

    async def _host_persist_runtime_state(self) -> None:
        method = getattr(self._g, "persist_runtime_state", None)
        if callable(method):
            await method()
            return
        method = self._legacy_attr("_persist_runtime_state")
        if callable(method):
            await method()

    async def _host_compact_session_history(self, *, force: bool = True) -> bool:
        method = getattr(self._g, "compact_session_history", None)
        if callable(method):
            return bool(await method(force=force))
        method = self._legacy_attr("_compact_session_history")
        return bool(await method(force=force))

    async def dispatch(self, inp: str) -> bool:
        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        async def set_plan() -> None:
            self._set_interaction_mode("plan")
            await self._host_persist_runtime_state()

        async def set_auto() -> None:
            self._set_interaction_mode("auto")
            await self._host_persist_runtime_state()

        def allow_tool() -> None:
            tool = args or cmd.removeprefix("/allow").strip()
            if tool:
                self._host_permission().allow(tool)

        def deny_tool() -> None:
            tool = args or cmd.removeprefix("/deny").strip()
            if tool:
                self._host_permission().deny(tool)

        async def compact() -> None:
            compacted = await self._host_compact_session_history(force=True)
            if compacted:
                ui.print("[dim]Compacted context.[/dim]")
            else:
                ui.print("[dim]Nothing to compact.[/dim]")

        def show_help() -> None:
            ui.print("[bold]Commands:[/bold]")
            for name, desc in COMMANDS:
                ui.print(f"  [cyan]{name}[/cyan] — {desc}")

        handlers = {
            "/exit": lambda: None,
            "/quit": lambda: None,
            "/clear": self._clear,
            "/code-ide": lambda: self._code_ide(args),
            "/list": self._list_sessions,
            "/resume": lambda: self._resume(inp),
            "/rollback": self._rollback,
            "/title": lambda: self._set_title(inp),
            "/mode": lambda: self._mode(args),
            "/goal": lambda: self._goal(args),
            "/guide": lambda: self._guide(args),
            "/init": lambda: self._init(args),
            "/lang": lambda: self._lang(args),
            "/plan": set_plan,
            "/unplan": set_auto,
            "/allow": allow_tool,
            "/deny": deny_tool,
            "/permissions": lambda: ui.print(self._host_permission().show_rules()),
            "/permission-mode": lambda: self._permission_mode(args),
            "/sandbox": lambda: self._sandbox(args),
            "/approval": lambda: self._approval(args),
            "/usage": self._usage,
            "/mcp": lambda: self._mcp(args),
            "/lsp": lambda: self._lsp(args),
            "/skills": lambda: self._skills(args),
            "/paste": self._paste_clipboard_image,
            "/tone": lambda: self._tone(args),
            "/parallel": lambda: self._parallel(args),
            "/debug": lambda: self._debug(args),
            "/compact": compact,
            "/diff": self._show_diff,
            "/tavily": lambda: self._tavily(args),
            "/model": lambda: self._dispatch_model(args),
            "/help": show_help,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return False

        result = handler()
        if isawaitable(result):
            await result
        return True

    async def _dispatch_model(self, args: str) -> None:
        if args == "new":
            await self._model_new()
        elif args == "list":
            await self._model_list()
        elif args == "test" or args.startswith("test "):
            target = args.removeprefix("test").strip()
            await self._model_test(target)
        elif args == "del" or args.startswith("del "):
            target = args.removeprefix("del").strip()
            await self._model_del(target)
        elif args == "switch" or args.startswith("switch "):
            target = args.removeprefix("switch").strip()
            await self._model_switch(target)
        elif args == "reasoning" or args.startswith("reasoning "):
            target = args.removeprefix("reasoning").strip()
            await self._model_reasoning(target)
        elif args:
            await self._switch_model(args)
        else:
            await self._model_switch("")

    def _set_interaction_mode(self, mode: str) -> None:
        from voidx.agent.runtime_context import InteractionMode

        parsed = InteractionMode.parse(mode)
        setter = getattr(self._g, "set_interaction_mode", None)
        if callable(setter):
            setter(parsed)
        else:
            self._set_legacy_attr("_plan_mode", parsed == InteractionMode.PLAN)
            self._set_legacy_attr("_interaction_mode", parsed)
        labels = {
            InteractionMode.AUTO: "Auto",
            InteractionMode.PLAN: "Plan",
            InteractionMode.GOAL: "Goal",
        }
        notes = {
            InteractionMode.PLAN: "write/edit/bash/lsp_format blocked",
            InteractionMode.GOAL: "keep work scoped to the current goal",
        }
        suffix = f" — {notes[parsed]}" if parsed in notes else ""
        ui.print(f"[dim]Mode set to [cyan]{labels[parsed]}[/cyan]{suffix}[/dim]")

    async def _mode(self, arg: str) -> None:
        from voidx.agent.runtime_context import InteractionMode

        mode = arg.strip().lower()
        choices = [
            ("Auto", InteractionMode.AUTO.value, "Infer the task intent from each turn."),
            ("Plan", InteractionMode.PLAN.value, "Read-only exploration and implementation planning."),
            ("Goal", InteractionMode.GOAL.value, "Keep multi-step work scoped to the current goal."),
        ]

        app = self._host_app()
        if not mode and app is not None:
            mode = await app.ask_choice("Interaction mode", choices) or ""

        if not mode:
            current = self._host_interaction_mode_value()
            ui.print(f"Mode: [cyan]{current}[/cyan]")
            ui.print("Usage: /mode [auto|plan|goal]")
            return

        try:
            parsed = InteractionMode.parse(mode)
        except ValueError:
            ui.error(f"Invalid mode: {mode}. Use: auto, plan, goal")
            return
        self._set_interaction_mode(parsed.value)
        await self._host_persist_runtime_state()

    async def _goal(self, arg: str) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun

        task_run = self._host_task_run()
        if task_run is None:
            task_run = TaskRun()
            self._set_host_task_run(task_run)

        goal = arg.strip()
        if goal.lower() in {"clear", "reset"}:
            task_run.clear()
            task_state = self._host_task_state()
            if task_state is not None:
                task_state.pending_approval = None
            self._set_interaction_mode(InteractionMode.AUTO.value)
            await self._host_persist_runtime_state()
            ui.print("[dim]Goal cleared.[/dim]")
            return

        if not goal:
            if task_run.goal:
                ui.print(
                    f"Goal: [cyan]{task_run.goal}[/cyan] "
                    f"[dim]({task_run.phase.value}, {task_run.status.value}, turns {task_run.turn_count})[/dim]"
                )
            else:
                ui.print("Usage: /goal <goal>|clear")
            return

        task_run.set_goal(goal)
        self._set_interaction_mode(InteractionMode.GOAL.value)
        await self._host_persist_runtime_state()
        ui.print(f"[dim]Goal set to [cyan]{task_run.goal}[/cyan][/dim]")

    def _usage(self) -> None:
        from voidx.llm.usage import format_cache_hit_rate, format_token_count

        stats = self._host_usage_stats()
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

    def _sandbox(self, arg: str) -> None:
        permission = self._host_permission()
        mode = arg.strip().lower()
        valid = {"read-only", "workspace-write", "danger-full-access"}
        if not mode:
            ui.print(f"Sandbox mode: [cyan]{permission.sandbox_mode}[/cyan]")
            ui.print("Usage: /sandbox [read-only|workspace-write|danger-full-access]")
            return
        if mode not in valid:
            ui.error(f"Invalid sandbox mode: {mode}. Use: {', '.join(valid)}")
            return
        permission.sandbox_mode = mode
        permission.mark_custom_mode()
        settings = self._host_settings()
        if settings is not None:
            from voidx.config import SandboxMode
            settings.set_sandbox_mode(SandboxMode(mode))
        ui.print(f"[dim]Sandbox mode set to [cyan]{mode}[/cyan][/dim]")

    def _approval(self, arg: str) -> None:
        permission = self._host_permission()
        policy = arg.strip().lower()
        valid = {"untrusted", "on-failure", "on-request", "never"}
        if not policy:
            ui.print(f"Approval policy: [cyan]{permission.approval_policy}[/cyan]")
            ui.print("Usage: /approval [untrusted|on-failure|on-request|never]")
            return
        if policy not in valid:
            ui.error(f"Invalid approval policy: {policy}. Use: {', '.join(valid)}")
            return
        permission.approval_policy = policy
        permission.mark_custom_mode()
        settings = self._host_settings()
        if settings is not None:
            from voidx.config import ApprovalPolicy
            settings.set_approval_policy(ApprovalPolicy(policy))
        ui.print(f"[dim]Approval policy set to [cyan]{policy}[/cyan][/dim]")

    async def _permission_mode(self, arg: str) -> None:
        from voidx.config import PermissionMode

        mode = arg.strip().lower()
        labels = {
            PermissionMode.DEFAULT.value: "Default",
            PermissionMode.READ_ONLY.value: "Read only",
            PermissionMode.ACCEPT_EDITS.value: "Accept edits",
            PermissionMode.AUTO_REVIEW.value: "Auto review",
            PermissionMode.FULL_ACCESS.value: "Full access",
            PermissionMode.CUSTOM.value: "Custom (.voidx/settings.json)",
        }
        valid = set(labels)

        app = self._host_app()
        if not mode and app is not None:
            choices = [
                (labels[PermissionMode.DEFAULT.value], PermissionMode.DEFAULT.value, "Ask before write/edit/bash."),
                (labels[PermissionMode.READ_ONLY.value], PermissionMode.READ_ONLY.value, "Block all writes and implement delegation."),
                (labels[PermissionMode.ACCEPT_EDITS.value], PermissionMode.ACCEPT_EDITS.value, "Allow workspace file edits; still ask for bash."),
                (labels[PermissionMode.AUTO_REVIEW.value], PermissionMode.AUTO_REVIEW.value, "Use reviewer-assisted approvals where possible."),
                (labels[PermissionMode.FULL_ACCESS.value], PermissionMode.FULL_ACCESS.value, "No sandbox or approval prompts."),
                (labels[PermissionMode.CUSTOM.value], PermissionMode.CUSTOM.value, "Use explicit sandbox/approval config."),
            ]
            mode = await app.ask_choice("Permission mode", choices) or ""

        permission = self._host_permission()
        if not mode:
            current = permission.permission_mode
            ui.print(f"Permission mode: [cyan]{labels.get(current, 'Custom')}[/cyan]")
            ui.print("Usage: /permission-mode [default|read-only|accept-edits|auto-review|full-access|custom]")
            return
        if mode not in valid:
            ui.error(f"Invalid permission mode: {mode}. Use: {', '.join(sorted(valid))}")
            return

        permission.set_permission_mode(mode)
        settings = self._host_settings()
        if settings is not None:
            settings.set_permission_mode(PermissionMode(mode))
        ui.print(f"[dim]Permission mode set to [cyan]{labels[mode]}[/cyan][/dim]")

    def _debug(self, arg: str) -> None:
        value = arg.strip().lower()
        if value in ("on", "true", "1", "yes"):
            self._g.set_debug(True)
        elif value in ("off", "false", "0", "no"):
            self._g.set_debug(False)
        elif value:
            ui.error("Usage: /debug [on|off]")
            return
        else:
            self._g.set_debug(not self._host_debug_enabled())

        state = "on" if self._host_debug_enabled() else "off"
        ui.print(f"[dim]debug {state}[/dim]")

    def _parallel(self, arg: str) -> None:
        settings = self._host_settings()
        if settings is None:
            ui.error("No settings available.")
            return

        value = arg.strip().lower()
        active = self._g.config.parallel_subagents
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
        app = self._host_app()
        if app is None or not hasattr(app, "paste_clipboard_image"):
            ui.error("/paste requires the interactive UI.")
            return
        result = app.paste_clipboard_image()
        if result.ok:
            ui.print(f"[dim]{result.message}[/dim]")
            return
        ui.error(result.message)

    async def _show_diff(self) -> None:
        workspace = self._host_workspace()
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

    async def _tavily(self, args: str) -> None:
        """Configure Tavily API key for web search."""
        settings = self._host_settings()
        if not settings:
            ui.error("No settings available.")
            return

        if not args or args.strip() == "show":
            key = settings.get_tavily_api_key()
            if key:
                ui.print(f"Tavily API key: [cyan]{self._mask_key(key)}[/cyan]")
            else:
                ui.print("[dim]Tavily API key not configured. Using DuckDuckGo fallback.[/dim]")
            ui.print("[dim]Usage: /tavily set | /tavily delete[/dim]")
            return

        parts = args.split(None, 1)
        action = parts[0].strip().lower() if parts else ""
        if action == "set":
            if len(parts) > 1 and parts[1].strip():
                ui.error("Do not include the API key in command text. Use /tavily set.")
                return
            api_key = await self._prompt("Tavily API key", default="", secret=True)
            if api_key is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            api_key = api_key.strip()
            if not api_key:
                ui.error("Tavily API key is required.")
                return
            settings.set_tavily_api_key(api_key)
            ui.print(f"Tavily API key saved: [cyan]{self._mask_key(api_key)}[/cyan]")
        elif args.strip() == "delete":
            settings.delete_tavily_api_key()
            ui.print("[dim]Tavily API key deleted. Using DuckDuckGo fallback.[/dim]")
        else:
            ui.print("[dim]Usage: /tavily [set|delete|show][/dim]")
