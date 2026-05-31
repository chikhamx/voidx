"""Slash command handler — extracted from graph.py to keep it focused."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.slash_components.code_ide import SlashCodeIdeMixin
from voidx.agent.slash_components.lsp import SlashLspMixin
from voidx.agent.slash_components.mcp import SlashMcpMixin
from voidx.agent.slash_components.model import SlashModelMixin
from voidx.agent.slash_components.skills import SlashSkillsMixin
from voidx.agent.slash_components.runtime import PROVIDERS, _select_from_list, _w, ui

if TYPE_CHECKING:
    from voidx.agent.graph import VoidXGraph


class SlashHandler(SlashCodeIdeMixin, SlashLspMixin, SlashSkillsMixin, SlashMcpMixin, SlashModelMixin):
    """Handles all slash commands (/help, /model, /plan, etc.).

    Takes a reference to the parent VoidXGraph since commands need access
    to session, config, permission, and model state.
    """

    def __init__(self, graph: VoidXGraph) -> None:
        self._g = graph

    async def dispatch(self, inp: str) -> bool:
        from voidx.ui.commands import COMMANDS

        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        known = [n for n, _ in COMMANDS if n == cmd]
        if not known:
            return False

        ui.print()

        if cmd in ("/exit", "/quit"):
            return True

        if cmd == "/clear":
            await self._clear()
        elif cmd == "/code-ide":
            await self._code_ide(args)
        elif cmd == "/list":
            await self._list_sessions()
        elif cmd.startswith("/resume"):
            await self._resume(inp)
        elif cmd.startswith("/title"):
            await self._set_title(inp)
        elif cmd == "/mode":
            await self._mode(args)
        elif cmd == "/goal":
            await self._goal(args)
        elif cmd == "/plan":
            self._set_interaction_mode("plan")
            if hasattr(self._g, "_persist_runtime_state"):
                await self._g._persist_runtime_state()
        elif cmd == "/unplan":
            self._set_interaction_mode("auto")
            if hasattr(self._g, "_persist_runtime_state"):
                await self._g._persist_runtime_state()
        elif cmd.startswith("/allow"):
            tool = args or cmd.removeprefix("/allow").strip()
            if tool:
                self._g._permission.allow(tool)
        elif cmd.startswith("/deny"):
            tool = args or cmd.removeprefix("/deny").strip()
            if tool:
                self._g._permission.deny(tool)
        elif cmd == "/permissions":
            ui.print(self._g._permission.show_rules())
        elif cmd == "/permission-mode":
            await self._permission_mode(args)
        elif cmd == "/sandbox":
            self._sandbox(args)
        elif cmd == "/approval":
            self._approval(args)
        elif cmd == "/usage":
            self._usage()
        elif cmd == "/mcp":
            await self._mcp(args)
        elif cmd == "/lsp":
            await self._lsp(args)
        elif cmd == "/skills":
            await self._skills(args)
        elif cmd == "/paste":
            self._paste_clipboard_image()
        elif cmd.startswith("/debug"):
            self._debug(args)
        elif cmd == "/compact":
            compacted = await self._g._compact_session_history(force=True)
            if compacted:
                ui.print("[dim]Compacted context.[/dim]")
            else:
                ui.print("[dim]Nothing to compact.[/dim]")
        elif cmd == "/diff":
            await self._show_diff()
        elif cmd == "/tavily":
            await self._tavily(args)
        elif cmd == "/model":
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
        elif cmd == "/help":
            ui.print("[bold]Commands:[/bold]")
            for name, desc in COMMANDS:
                ui.print(f"  [cyan]{name}[/cyan] — {desc}")
        return True

    def _set_interaction_mode(self, mode: str) -> None:
        from voidx.agent.runtime_context import InteractionMode

        parsed = InteractionMode.parse(mode)
        setter = getattr(self._g, "set_interaction_mode", None)
        if callable(setter):
            setter(parsed)
        else:
            self._g._plan_mode = parsed == InteractionMode.PLAN
            self._g._interaction_mode = parsed
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

        if not mode and getattr(self._g, "_app", None):
            mode = await self._g._app.ask_choice("Interaction mode", choices) or ""

        if not mode:
            current = getattr(getattr(self._g, "_interaction_mode", None), "value", None)
            if current is None:
                current = "plan" if getattr(self._g, "_plan_mode", False) else "auto"
            ui.print(f"Mode: [cyan]{current}[/cyan]")
            ui.print("Usage: /mode [auto|plan|goal]")
            return

        try:
            parsed = InteractionMode.parse(mode)
        except ValueError:
            ui.error(f"Invalid mode: {mode}. Use: auto, plan, goal")
            return
        self._set_interaction_mode(parsed.value)
        if hasattr(self._g, "_persist_runtime_state"):
            await self._g._persist_runtime_state()

    async def _goal(self, arg: str) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun

        task_run = getattr(self._g, "_task_run", None)
        if task_run is None:
            task_run = TaskRun()
            self._g._task_run = task_run

        goal = arg.strip()
        if goal.lower() in {"clear", "reset"}:
            task_run.clear()
            task_state = getattr(self._g, "_task_state", None)
            if task_state is not None:
                task_state.awaiting_implementation_approval = False
                task_state.approved_scope = ""
            self._set_interaction_mode(InteractionMode.AUTO.value)
            if hasattr(self._g, "_persist_runtime_state"):
                await self._g._persist_runtime_state()
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
        if hasattr(self._g, "_persist_runtime_state"):
            await self._g._persist_runtime_state()
        ui.print(f"[dim]Goal set to [cyan]{task_run.goal}[/cyan][/dim]")

    def _usage(self) -> None:
        from voidx.llm.usage import format_cache_hit_rate, format_token_count

        stats = getattr(self._g, "_usage_stats", None)
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
        mode = arg.strip().lower()
        valid = {"read-only", "workspace-write", "danger-full-access"}
        if not mode:
            ui.print(f"Sandbox mode: [cyan]{self._g._permission.sandbox_mode}[/cyan]")
            ui.print("Usage: /sandbox [read-only|workspace-write|danger-full-access]")
            return
        if mode not in valid:
            ui.error(f"Invalid sandbox mode: {mode}. Use: {', '.join(valid)}")
            return
        self._g._permission.sandbox_mode = mode
        self._g._permission.mark_custom_mode()
        if getattr(self._g, "_settings", None):
            from voidx.config import SandboxMode
            self._g._settings.set_sandbox_mode(SandboxMode(mode))
        ui.print(f"[dim]Sandbox mode set to [cyan]{mode}[/cyan][/dim]")

    def _approval(self, arg: str) -> None:
        policy = arg.strip().lower()
        valid = {"untrusted", "on-failure", "on-request", "never"}
        if not policy:
            ui.print(f"Approval policy: [cyan]{self._g._permission.approval_policy}[/cyan]")
            ui.print("Usage: /approval [untrusted|on-failure|on-request|never]")
            return
        if policy not in valid:
            ui.error(f"Invalid approval policy: {policy}. Use: {', '.join(valid)}")
            return
        self._g._permission.approval_policy = policy
        self._g._permission.mark_custom_mode()
        if getattr(self._g, "_settings", None):
            from voidx.config import ApprovalPolicy
            self._g._settings.set_approval_policy(ApprovalPolicy(policy))
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
            PermissionMode.CUSTOM.value: "Custom (voidx.json)",
        }
        valid = set(labels)

        if not mode and getattr(self._g, "_app", None):
            choices = [
                (labels[PermissionMode.DEFAULT.value], PermissionMode.DEFAULT.value, "Ask before write/edit/bash."),
                (labels[PermissionMode.READ_ONLY.value], PermissionMode.READ_ONLY.value, "Block all writes and implement delegation."),
                (labels[PermissionMode.ACCEPT_EDITS.value], PermissionMode.ACCEPT_EDITS.value, "Allow workspace file edits; still ask for bash."),
                (labels[PermissionMode.AUTO_REVIEW.value], PermissionMode.AUTO_REVIEW.value, "Use reviewer-assisted approvals where possible."),
                (labels[PermissionMode.FULL_ACCESS.value], PermissionMode.FULL_ACCESS.value, "No sandbox or approval prompts."),
                (labels[PermissionMode.CUSTOM.value], PermissionMode.CUSTOM.value, "Use explicit sandbox/approval config."),
            ]
            mode = await self._g._app.ask_choice("Permission mode", choices) or ""

        if not mode:
            current = self._g._permission.permission_mode
            ui.print(f"Permission mode: [cyan]{labels.get(current, 'Custom')}[/cyan]")
            ui.print("Usage: /permission-mode [default|read-only|accept-edits|auto-review|full-access|custom]")
            return
        if mode not in valid:
            ui.error(f"Invalid permission mode: {mode}. Use: {', '.join(sorted(valid))}")
            return

        self._g._permission.set_permission_mode(mode)
        if getattr(self._g, "_settings", None):
            self._g._settings.set_permission_mode(PermissionMode(mode))
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
            self._g.set_debug(not self._g._debug)

        state = "on" if self._g._debug else "off"
        ui.print(f"[dim]debug {state}[/dim]")

    def _paste_clipboard_image(self) -> None:
        app = getattr(self._g, "_app", None)
        if app is None or not hasattr(app, "paste_clipboard_image"):
            ui.error("/paste requires the interactive UI.")
            return
        result = app.paste_clipboard_image()
        if result.ok:
            ui.print(f"[dim]{result.message}[/dim]")
            return
        ui.error(result.message)

    async def _show_diff(self) -> None:
        from voidx.ui.diff import git_diff, git_diff_stat
        stat = git_diff_stat(self._g._workspace)
        if stat:
            ui.print(f"[bold]Changes:[/bold]\n{stat}\n")
            diff_text = git_diff(self._g._workspace)
            if diff_text:
                ui.diff(diff_text)
            else:
                ui.print("[dim]No diff content.[/dim]")
        else:
            ui.print("[dim]No changes in working tree.[/dim]")

    async def _clear(self) -> None:
        if self._g._session:
            from voidx.memory.session import clear_messages, update_title
            await clear_messages(self._g._session.id)
            await update_title(self._g._session.id, "New session")
            if hasattr(self._g, "_clear_runtime_state"):
                await self._g._clear_runtime_state()
            self._g._session = self._g._session.model_copy(update={
                "title": "New session",
                "message_count": 0,
            })
            self._g._tracker._todos = []
            self._g._permission.clear_session_permissions()
            stats = getattr(self._g, "_usage_stats", None)
            if stats is not None:
                stats.reset()
        from voidx.ui.session_changes import session_tracker
        session_tracker.clear()
        from voidx.ui.dock import get_dock
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._g._show_startup()
        ui.print("[dim]✓ Session cleared — ready for a new conversation[/dim]")

    async def _list_sessions(self) -> None:
        from voidx.memory.session import list_sessions
        sessions = await list_sessions()
        if not sessions:
            ui.print("No saved sessions.")
            return

        ui.print("[bold]Sessions:[/bold]")
        items = []
        for s in sessions:
            title = s.title[:50] + ("..." if len(s.title) > 50 else "")
            items.append(f"{s.id[:8]} | {title} | {getattr(s, 'updated_at', '')[:16]}")
        
        idx = None
        if getattr(self._g, "_app", None):
            idx = await _select_from_list(self._g._app, "Resume session?", items)
        
        if idx is not None:
            await self._resume(f"/resume {sessions[idx].id}")

    async def _resume(self, cmd: str) -> None:
        from voidx.memory.session import get_session
        sid = cmd.removeprefix("/resume").strip()
        if not sid:
            ui.error("Usage: /resume <session_id>")
            return
        session = await get_session(sid)
        if not session:
            ui.error(f"Session not found: {sid}")
            return
        self._g._session = session
        self._g._workspace = session.workspace
        self._g.config.workspace = session.workspace
        if hasattr(self._g, "_restore_runtime_state"):
            await self._g._restore_runtime_state()
        from voidx.ui.dock import get_dock
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._g._show_startup(append_transcript=True)
        ui.print(f"[dim]Resumed: {session.id} — {session.title} ({session.message_count} msgs)[/dim]")

    async def _set_title(self, cmd: str) -> None:
        from voidx.memory.session import update_title
        if not self._g._session:
            return
        title = cmd.removeprefix("/title").strip()
        if title:
            await update_title(self._g._session.id, title)
            ui.print(f"[dim]Title set: {title}[/dim]")

    async def _tavily(self, args: str) -> None:
        """Configure Tavily API key for web search."""
        settings = self._g._settings
        if not settings:
            ui.error("No settings available.")
            return

        if not args or args.strip() == "show":
            key = settings.get_tavily_api_key()
            if key:
                masked = key[:4] + "****" + key[-4:] if len(key) > 8 else key
                ui.print(f"Tavily API key: [cyan]{masked}[/cyan]")
            else:
                ui.print("[dim]Tavily API key not configured. Using DuckDuckGo fallback.[/dim]")
            ui.print("[dim]Usage: /tavily set <api_key> | /tavily delete[/dim]")
            return

        if args.startswith("set "):
            api_key = args[4:].strip()
            if api_key:
                settings.set_tavily_api_key(api_key)
                masked = api_key[:4] + "****" + api_key[-4:] if len(api_key) > 8 else api_key
                ui.print(f"Tavily API key saved: [cyan]{masked}[/cyan]")
            else:
                ui.error("Usage: /tavily set <api_key>")
        elif args.strip() == "delete":
            settings.delete_tavily_api_key()
            ui.print("[dim]Tavily API key deleted. Using DuckDuckGo fallback.[/dim]")
        else:
            ui.print("[dim]Usage: /tavily [set <api_key>|delete|show][/dim]")
