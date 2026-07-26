"""Slash command handler — extracted from graph.py to keep it focused."""

from __future__ import annotations

from voidx.config import CodeIde, McpServerConfig, WebToolRoute
from voidx.runtime.ui import code_ide_status, detect_code_ides, normalize_ide, ui
from voidx.runtime.ui import ui
from pathlib import Path
from voidx.runtime.intent import InteractionMode
from voidx.lsp.config import lsp_config_path
from voidx.agent.slash.runtime import _select_from_list
from voidx.runtime.ui import get_dock, session_tracker, ui
from voidx.skills.service import SkillRegistry, SkillService
import time
from voidx.selfupdate import UpgradeResult, check_for_update, is_newer, perform_upgrade, upgrade_hint
import os
import shlex
import sys
from collections.abc import Mapping
import re
from voidx.agent.loop.prompt_source import PromptSource
from voidx.tools.service import BashTool, ToolContext
from voidx.config import UserProfile
from voidx.agent.slash.runtime import PROVIDERS, get_providers, _select_from_list

import asyncio
from inspect import isawaitable
from typing import Any

from voidx.diffing import git_diff, git_diff_stat
from voidx.agent.slash.runtime import PROVIDERS, _select_from_list, _w, prompt_text
from voidx.runtime.ui import COMMANDS, paste_clipboard_image, ui


def _ide_label(value: str) -> str:
    labels = {
        CodeIde.AUTO.value: "Auto",
        CodeIde.TRAE.value: "Trae",
        CodeIde.CURSOR.value: "Cursor",
        CodeIde.CODE.value: "VS Code",
        CodeIde.WINDSURF.value: "Windsurf",
        CodeIde.ZED.value: "Zed",
        CodeIde.SUBLIME.value: "Sublime Text",
        CodeIde.JETBRAINS.value: "JetBrains",
        CodeIde.GHOSTTY.value: "Ghostty",
        CodeIde.SYSTEM.value: "System default",
    }
    return labels.get(value, value)

INIT_PROMPT = """\
Generate an AGENTS.md file for this project. Write it to the workspace root.

## What to do

1. Scan the project structure using glob, grep, and read tools.
2. Detect the language, framework, test runner, linter, and build system.
3. Read key config files such as pyproject.toml, package.json, Cargo.toml,
   go.mod, Makefile, justfile, and README files to extract exact commands.
4. Write AGENTS.md to the workspace root.

## AGENTS.md Structure

Follow this section order. Keep the file concise: rules and facts, not essays.

### Project Shape

List top-level directories with their purpose. Be specific. For example,
describe the kind of source code, runtime layer, frontend, tests, scripts, or
docs each directory contains.

### Commands

List exact commands to run, test, lint, format, build, and start dev servers.
Use the actual package manager and flags detected from config files. Include a
full test command and a focused test command when possible.

### Code Rules

Infer conventions from code and config. Look at formatter/linter config,
typing config, module naming, import style, and existing local patterns.

### voidx Integration

Include only rules that help voidx agents work effectively with this project.
Cover:

- Workflow skills: mention only skills that exist in the local skill registry
  or are already referenced by this project.
- Permission awareness: note write/edit/bash/agent(implement)
  approval expectations when relevant.

### Document Lifecycle

If the project has design docs or specs, document where in-progress docs live,
when they move to archive, and what counts as complete.

### Safety

Include safety rules relevant to the project, especially:

- Do not commit local credentials, .env files, .voidx, generated secrets, or
  other local-only state.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run focused verification before broader test runs.
- Do not run destructive commands unless the user explicitly asks.

## Rules

- Use tools to discover facts. Do not guess commands or structure.
- Read actual config files before writing command recommendations.
- If an existing AGENTS.md is present because /init force was used, read it
  first and preserve custom project-specific sections when they are still
  accurate.
- Do not include made-up skills, tools, scripts, or commands.
- Keep the generated AGENTS.md practical and concise.
"""

def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(max(value, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024

def _format_timestamp(value: int | None) -> str:
    if value is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))

def _format_upgrade_success(result: UpgradeResult) -> str:
    if result.version is None:
        return result.message
    return f"[green]{result.message}[/green]"

def _parse_env_pairs(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result

_INTERVAL_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")

_TRAILING_EVERY_RE = re.compile(r"\s+every\s+(?P<value>\d+)(?P<unit>[smhd])\s*$")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

def _parse_interval(args: str) -> tuple[float | None, str]:
    parts = args.split(None, 1)
    if parts:
        match = _INTERVAL_RE.match(parts[0])
        if match:
            prompt = parts[1] if len(parts) > 1 else ""
            return _interval_seconds(match), prompt
    match = _TRAILING_EVERY_RE.search(args)
    if match:
        prompt = args[:match.start()].strip()
        return _interval_seconds(match), prompt
    return None, args

def _interval_seconds(match: re.Match[str]) -> float:
    seconds = int(match.group("value")) * _UNIT_SECONDS[match.group("unit")]
    return float(max(seconds, 60))

def _tool_context_for_host(host) -> ToolContext:
    permission = getattr(host, "permission", None)
    workspace = getattr(host, "workspace", ".")
    session = getattr(host, "session", None)
    kwargs = {
        "workspace": workspace,
        "session_id": getattr(session, "id", "default") or "default",
        "loop_manager": getattr(host, "loop_manager", None),
        "tool_registry": getattr(host, "tools", None),
        "format_after_edit_enabled": getattr(getattr(host, "config", None), "lsp_format_after_edit", True),
    }
    if permission is not None:
        kwargs.update(
            permission_mode=getattr(permission, "permission_mode", "safe"),
            sandbox_readable_files=list(getattr(permission, "sandbox_readable_files", [])),
            sandbox_readable_dirs=list(getattr(permission, "sandbox_readable_dirs", [])),
            sandbox_writable_files=list(getattr(permission, "sandbox_writable_files", [])),
            sandbox_writable_dirs=list(getattr(permission, "sandbox_writable_dirs", [])),
            get_access_grants=getattr(permission, "get_access_grants", None),
            get_revocation_epoch=lambda: getattr(permission, "revocation_epoch", 0),
            add_grant=getattr(permission, "add_grant", None),
            acquire_grant_targets=getattr(permission, "acquire_grant_targets", None),
            acquire_execution_lease=getattr(permission, "execution_lease_for_tool", None),
            process_sandbox=getattr(permission, "process_sandbox", None),
        )
    return ToolContext(**kwargs)

def _normalize_language(value: str) -> str:
    text = value.strip()
    if text.lower() in {"", "auto", "detect", "default"}:
        return ""
    return text

def _normalize_tone(value: str) -> str:
    text = value.strip()
    if text.lower() in {"", "auto", "default"}:
        return ""
    return text

class SlashHandler:
    """Handles all slash commands (/help, /model, /plan, etc.).

    Takes a reference to the parent LangGraphExecution since commands need access
    to session, config, permission, and model state.
    """

    def __init__(self, commands: Any) -> None:
        self.host = commands

    async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
        return await prompt_text(self.host.app, text, default=default, secret=secret)

    async def dispatch(self, inp: str) -> bool:
        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        async def set_plan() -> None:
            self._set_interaction_mode("plan")
            await self.host.persist_runtime_state()

        async def set_auto() -> None:
            self._set_interaction_mode("auto")
            await self.host.persist_runtime_state()

        def allow_tool() -> None:
            tool = args or cmd.removeprefix("/allow").strip()
            if tool:
                self.host.permission.allow(tool)

        def deny_tool() -> None:
            tool = args or cmd.removeprefix("/deny").strip()
            if tool:
                self.host.permission.deny(tool)

        async def compact() -> None:
            compacted = await self.host.compact_session_history(force=True)
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
            "/session": lambda: self._session(args),
            "/chat": lambda: self._chat_shortcut(args),
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
            "/permissions": lambda: ui.print(self.host.permission.show_rules()),
            "/permission": lambda: self._permission_mode(args),
            "/usage": self._usage,
            "/upgrade": lambda: self._upgrade(args),
            "/mcp": lambda: self._mcp(args),
            "/lsp": lambda: self._lsp(args),
            "/loop": lambda: self._loop(args),
            "/skills": lambda: self._skills(args),
            "/paste": self._paste_clipboard_image,
            "/tone": lambda: self._tone(args),
            "/parallel": lambda: self._parallel(args),
            "/debug": lambda: self._debug(args),
            "/log": lambda: self._log(args),
            "/compact": compact,
            "/diff": self._show_diff,
            "/tavily": lambda: self._tavily(args),
            "/bocha": lambda: self._bocha(args),
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
        elif args == "ctx" or args.startswith("ctx "):
            target = args.removeprefix("ctx").strip()
            await self._model_ctx(target)
        elif args:
            await self._model_switch(args)
        else:
            await self._model_switch("")

    def _set_interaction_mode(self, mode: str) -> None:
        from voidx.agent.runtime_context import InteractionMode

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

    async def _mode(self, arg: str) -> None:
        from voidx.agent.runtime_context import InteractionMode

        mode = arg.strip().lower()
        choices = [
            ("Auto", InteractionMode.AUTO.value, "Infer the task intent from each turn."),
            ("Plan", InteractionMode.PLAN.value, "Read-only exploration and implementation planning."),
            ("Goal", InteractionMode.GOAL.value, "Keep multi-step work scoped to the current goal."),
        ]

        app = self.host.app
        if not mode and app is not None:
            mode = await app.ask_choice("Interaction mode", choices) or ""

        if not mode:
            current = self.host.interaction_mode_value()
            ui.print(f"Mode: [cyan]{current}[/cyan]")
            ui.print("Usage: /mode [auto|plan|goal]")
            return

        try:
            parsed = InteractionMode.parse(mode)
        except ValueError:
            ui.error(f"Invalid mode: {mode}. Use: auto, plan, goal")
            return
        self._set_interaction_mode(parsed.value)
        await self.host.persist_runtime_state()

    async def _goal(self, arg: str) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.runtime.task_state import TaskState, goal_label

        task_state = self.host.task_state or TaskState()
        goal = arg.strip()
        if goal.lower() in {"clear", "reset"}:
            task_state.clear_goal()
            self.host.set_task_state(task_state)
            self._set_interaction_mode(InteractionMode.AUTO.value)
            await self.host.persist_runtime_state()
            ui.print("[dim]Goal cleared.[/dim]")
            return

        if not goal:
            if task_state.current_goal is not None:
                ui.print(f"Goal: [cyan]{goal_label(task_state.current_goal)}[/cyan]")
            else:
                ui.print("Usage: /goal <goal>|clear")
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


    async def _permission_mode(self, arg: str) -> None:
        from voidx.config import AiApprovalConfig, PermissionMode

        parts = arg.strip().split(None, 1)
        raw = parts[0].lower().replace("-", "_") if parts else ""
        requested_profile = parts[1].strip() if len(parts) > 1 else None
        labels = {
            PermissionMode.READ_ONLY.value: "Read only",
            PermissionMode.SAFE.value: "Safe",
            PermissionMode.AI_APPROVAL.value: "AI approval",
            PermissionMode.PROJECT_TRUSTED.value: "Project trusted",
            PermissionMode.FULL_ACCESS.value: "Full access",
        }
        choices = [
            (labels[PermissionMode.READ_ONLY.value], PermissionMode.READ_ONLY.value, "Ask for writes and block/acknowledge unsafe operations."),
            (labels[PermissionMode.SAFE.value], PermissionMode.SAFE.value, "Ask before writes or risky commands."),
            (labels[PermissionMode.AI_APPROVAL.value], PermissionMode.AI_APPROVAL.value, "AI pre-screens dangerous tools; uncertain calls still ask you."),
            (labels[PermissionMode.PROJECT_TRUSTED.value], PermissionMode.PROJECT_TRUSTED.value, "Allow workspace edits; ask for broader risk."),
            (labels[PermissionMode.FULL_ACCESS.value], PermissionMode.FULL_ACCESS.value, "Allow most operations; still ask for extreme risk."),
        ]
        valid = set(labels)

        app = self.host.app
        if not raw and app is not None:
            raw = await app.ask_choice("Permission mode", choices) or ""

        if not raw:
            current = getattr(self.host.permission, "permission_mode", PermissionMode.SAFE.value)
            ui.print(f"Permission mode: [cyan]{labels.get(current, labels[PermissionMode.SAFE.value])}[/cyan]")
            ui.print("Usage: /permission [read_only|safe|ai_approval [profile]|project_trusted|full_access]")
            return
        if raw not in valid:
            ui.error(f"Invalid permission mode: {raw}. Use: {', '.join(sorted(valid))}")
            return

        settings = self.host.settings
        selected_profile: str | None = None
        if raw == PermissionMode.AI_APPROVAL.value and settings is not None:
            profiles = [profile for profile in await settings.list_profiles() if profile.api_key]
            if requested_profile is not None:
                match = next((profile for profile in profiles if profile.name == requested_profile), None)
                if match is None:
                    ui.error(f"Unknown or unconfigured AI approval profile: {requested_profile}")
                    return
                selected_profile = match.name
            elif app is not None:
                profile_choices = [
                    ("Current main profile (default)", "", "Follow the active model profile."),
                    *((profile.name, profile.name, "") for profile in profiles),
                ]
                selected_profile = await app.ask_choice("AI approval profile", profile_choices)

        preset = PermissionMode(raw)
        try:
            self.host.permission.set_permission_mode(preset.value)
        except PermissionError as exc:
            ui.error(str(exc))
            return
        self.host.clear_successful_dangerous_calls()
        if settings is not None:
            settings.set_permission_mode(preset)
            if selected_profile is not None:
                current = settings.get_ai_approval_config()
                settings.set_ai_approval_config(AiApprovalConfig(
                    profile_name=selected_profile,
                    timeout_seconds=current.timeout_seconds,
                ))
        suffix = f" using {selected_profile or 'current main profile'}" if selected_profile is not None else ""
        ui.print(f"[dim]Permission mode set to [cyan]{labels[preset.value]}[/cyan]{suffix}[/dim]")

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

    async def _tavily(self, args: str) -> None:
        """Configure Tavily API key for web search."""
        settings = self.host.settings
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
            await self._sync_tavily_mcp_config(api_key)
            ui.print(f"Tavily API key saved: [cyan]{self._mask_key(api_key)}[/cyan]")
            ui.print("[dim]Tavily MCP server configured for websearch/webfetch.[/dim]")
        elif args.strip() == "delete":
            settings.delete_tavily_api_key()
            await self._remove_tavily_mcp_key()
            ui.print("[dim]Tavily API key deleted. Using DuckDuckGo fallback.[/dim]")
        else:
            ui.print("[dim]Usage: /tavily [set|delete|show][/dim]")
    async def _bocha(self, args: str) -> None:
        """Configure Bocha API key for web search."""
        settings = self.host.settings
        if not settings:
            ui.error("No settings available.")
            return
        if not args or args.strip() == "show":
            key = settings.get_bocha_api_key()
            if key:
                ui.print(f"Bocha API key: [cyan]{self._mask_key(key)}[/cyan]")
            else:
                ui.print("[dim]Bocha API key not configured. Using crawler fallbacks.[/dim]")
            ui.print("[dim]Usage: /bocha set | /bocha delete[/dim]")
            return
        action = args.split(None, 1)[0].strip().lower()
        if action == "set":
            api_key = await self._prompt("Bocha API key", default="", secret=True)
            if api_key is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            api_key = api_key.strip()
            if not api_key:
                ui.error("Bocha API key is required.")
                return
            settings.set_bocha_api_key(api_key)
            ui.print(f"Bocha API key saved: [cyan]{self._mask_key(api_key)}[/cyan]")
        elif args.strip() == "delete":
            settings.delete_bocha_api_key()
            ui.print("[dim]Bocha API key deleted. Using crawler fallbacks.[/dim]")
        else:
            ui.print("[dim]Usage: /bocha [set|delete|show][/dim]")


    async def _sync_tavily_mcp_config(self, api_key: str) -> None:
        from voidx.config import McpServerConfig, WebToolRoute

        settings = self.host.settings
        if settings is None:
            return
        existing = settings.get_mcp_server("tavily")
        if existing is None:
            server = McpServerConfig(
                name="tavily",
                command="npx",
                args=["-y", "tavily-mcp@latest"],
                env={"TAVILY_API_KEY": api_key},
                tools=["tavily_search", "tavily_extract"],
            )
        else:
            server = existing.model_copy(
                update={"env": {**existing.env, "TAVILY_API_KEY": api_key}},
            )
        settings.save_mcp_server(server)
        settings.set_web_tool_route(
            "search",
            WebToolRoute(backend="mcp", server="tavily", tool="tavily_search"),
        )
        settings.set_web_tool_route(
            "fetch",
            WebToolRoute(backend="mcp", server="tavily", tool="tavily_extract"),
        )
        await self._restart_mcp_manager_if_available()

    async def _remove_tavily_mcp_key(self) -> None:
        settings = self.host.settings
        if settings is None:
            return
        existing = settings.get_mcp_server("tavily")
        if existing is not None and "TAVILY_API_KEY" in existing.env:
            env = dict(existing.env)
            env.pop("TAVILY_API_KEY", None)
            settings.save_mcp_server(existing.model_copy(update={"env": env}))
        settings.clear_web_routes_for_server("tavily", save=True)
        await self._restart_mcp_manager_if_available()

    async def _restart_mcp_manager_if_available(self) -> None:
        manager = self.host.mcp_manager
        if manager is None:
            return
        try:
            await asyncio.wait_for(manager.restart_all(), timeout=30.0)
        except asyncio.TimeoutError:
            ui.warn("MCP restart timed out; servers may still be connecting in the background.")

    async def _code_ide(self, args: str) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings file available.")
            return

        value = args.strip().lower()
        if value == "status":
            ui.print(code_ide_status(settings))
            return

        valid = {item.value for item in CodeIde}
        if not value:
            app = self.host.app
            if app is not None:
                detected = detect_code_ides()
                detected_ids = {item.id for item in detected}
                choices = []
                for ide in CodeIde:
                    label = _ide_label(ide.value)
                    desc = "configured default" if ide.value == settings.get_code_ide().value else ""
                    if ide.value in detected_ids:
                        desc = (desc + " · " if desc else "") + "detected"
                    elif ide.value not in {CodeIde.AUTO.value, CodeIde.SYSTEM.value}:
                        desc = (desc + " · " if desc else "") + "not detected"
                    choices.append((label, ide.value, desc))
                selected = await app.ask_choice("Code IDE", choices)
                if selected:
                    value = selected
            if not value:
                ui.print(code_ide_status(settings))
                ui.print("Usage: /code-ide [auto|trae|cursor|code|windsurf|zed|sublime|jetbrains|ghostty|system|status]")
                return

        value = normalize_ide(value)
        if value not in valid:
            ui.error(f"Invalid code IDE: {value}. Use: {', '.join(sorted(valid))}")
            return

        path = settings.set_code_ide(CodeIde(value))
        ui.print(f"[dim]Code IDE set to [cyan]{value}[/cyan]. Saved to {path}[/dim]")
        ui.print(code_ide_status(settings))

    async def _guide(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            ui.print("[dim]Usage: /guide <guidance for the next agent step>[/dim]")
            return
        if not self.host.can_submit_guidance():
            ui.print("[dim]Guidance is not available in this session.[/dim]")
            return
        if not self.host.submit_guidance(guidance):
            ui.print("[dim]No guidance submitted.[/dim]")

    async def _init(self, args: str) -> None:
        arg = args.strip().lower()
        if arg not in {"", "force"}:
            ui.error("Usage: /init [force]")
            return

        if self.host.interaction_mode_value() == InteractionMode.PLAN.value:
            ui.error("/init writes AGENTS.md. Run /unplan first.")
            return

        existing = Path(self.host.workspace) / "AGENTS.md"
        if existing.exists() and arg != "force":
            ui.print("[dim]AGENTS.md already exists. Use /init force to regenerate.[/dim]")
            return

        await self.host.run_coding_turn(INIT_PROMPT, display_text="/init")

    async def _lsp(self, args: str) -> None:
        parts = args.split(None, 1)
        action = parts[0] if parts else "status"
        target = parts[1].strip() if len(parts) > 1 else ""

        if action in ("", "status"):
            self._lsp_status()
        elif action == "doctor":
            self._lsp_doctor()
        elif action == "restart":
            await self._lsp_restart(target or None)
        elif action == "servers":
            self._lsp_servers()
        else:
            ui.error("Usage: /lsp [status|doctor|restart|servers]")

    def _lsp_status(self) -> None:
        manager = self.host.lsp_manager
        if manager is None:
            ui.error("No LSP manager available.")
            return
        ui.print("[bold]LSP status:[/bold]")
        for status in manager.statuses():
            label = {
                "initializing": "[dim]initializing[/dim]",
                "connected": "[green]connected[/green]",
                "disconnected": "[dim]disconnected[/dim]",
                "disabled": "[dim]disabled[/dim]",
                "error": "[red]error[/red]",
            }.get(status.status, status.status)
            detail = f" · pid {status.pid}" if status.pid else ""
            docs = f" · {status.open_documents} doc{'s' if status.open_documents != 1 else ''}"
            ui.print(f"  [cyan]{status.language}[/cyan] · {label}{detail}{docs}")
            if status.error_message:
                ui.print(f"    [red]{status.error_message}[/red]")
        ui.print("[dim]Usage: /lsp status|doctor|restart|servers[/dim]")

    def _lsp_doctor(self) -> None:
        manager = self.host.lsp_manager
        if manager is None:
            ui.error("No LSP manager available.")
            return
        if getattr(manager, "initializing", False) or not getattr(manager, "initialized", True):
            ui.print("[dim]LSP servers are still initializing.[/dim]")
            return
        ui.print("[bold]LSP doctor:[/bold]")
        missing = 0
        disabled = 0
        auto_detected = 0
        for check in manager.doctor():
            if not check.enabled:
                disabled += 1
                ui.print(f"  [cyan]{check.language}[/cyan] · [dim]disabled[/dim] · {check.command}")
                continue
            source = f" [dim]({check.detected_source})[/dim]" if check.detected_source else ""
            if check.available:
                if check.detected_source:
                    auto_detected += 1
                ui.print(
                    f"  [cyan]{check.language}[/cyan] · [green]ok[/green] · "
                    f"{check.command} [dim]({check.resolved_path})[/dim]{source}"
                )
                continue
            missing += 1
            ui.print(f"  [cyan]{check.language}[/cyan] · [red]missing[/red] · {check.command}")
            if check.install_hint:
                ui.print(f"    [dim]{check.install_hint}[/dim]")
        if missing:
            ui.print(f"[yellow]{missing} LSP server{'s' if missing != 1 else ''} missing.[/yellow]")
        elif disabled:
            ui.print("[dim]No missing enabled LSP servers.[/dim]")
        else:
            msg = "All enabled LSP servers are available."
            if auto_detected:
                msg += f" ({auto_detected} auto-detected)"
            ui.print(f"[green]{msg}[/green]")

    async def _lsp_restart(self, language: str | None) -> None:
        manager = self.host.lsp_manager
        if manager is None:
            ui.error("No LSP manager available.")
            return
        await manager.restart(language)
        target = language or "all servers"
        ui.print(f"[green]✓ restarted {target}[/green]")

    def _lsp_servers(self) -> None:
        manager = self.host.lsp_manager
        workspace = self.host.workspace
        ui.print("[bold]LSP servers:[/bold]")
        ui.print(f"[dim]{lsp_config_path(workspace)}[/dim]")
        if manager is None:
            ui.error("No LSP manager available.")
            return
        if getattr(manager, "initializing", False) or not getattr(manager, "initialized", True):
            ui.print("[dim]LSP servers are still initializing.[/dim]")
            return
        for config in manager.servers.values():
            state = "[green]enabled[/green]" if config.enabled else "[dim]disabled[/dim]"
            exts = ", ".join(config.extensions) or "no extensions"
            command = " ".join([config.command, *config.args]).strip()
            ui.print(f"  [cyan]{config.language}[/cyan] · {state} · [dim]{exts}[/dim]")
            ui.print(f"    {command}")

    async def _session(self, args: str) -> None:
        subcommand, _, rest = args.strip().partition(" ")
        if subcommand in {"list", "ls"}:
            await self._list_sessions()
            return
        if subcommand == "new":
            profile = None
            rest_lower = rest.strip().lower()
            if rest_lower in {"chat", "--chat", "-c"}:
                profile = "chat"
            elif rest_lower in {"coding", "--coding"}:
                profile = "coding"

            if profile is None:
                await self._clear()
                return

            from voidx.memory.service import create_session
            config = getattr(self.host, "config", None)
            model_info = getattr(config, "model", None) if config else None
            provider = getattr(model_info, "provider", "anthropic") if model_info else "anthropic"
            model = getattr(model_info, "model", "claude-3-5-sonnet") if model_info else "claude-3-5-sonnet"
            workspace = getattr(self.host, "workspace", "")

            session = await create_session(
                workspace=workspace,
                provider=provider,
                model=model,
                profile=profile,
                title="Chat session" if profile == "chat" else "New session",
            )

            if hasattr(self.host, "resume_session"):
                await self.host.resume_session(session)
            session_tracker.clear()
            active_dock = get_dock()
            if active_dock is not None:
                active_dock.reset()
            await self._show_startup(prefer_direct=True)
            return

        if subcommand == "resume":
            await self._resume(f"/resume {rest}".rstrip())
            return
        if subcommand in {"del", "delete"}:
            await self._session_del(rest)
            return
        ui.print("[dim]Usage: /session list|new|resume|del[/dim]")

    async def _chat_shortcut(self, args: str) -> None:
        await self._session(f"new chat {args}".strip())


    async def _session_del(self, args: str) -> None:
        parts = args.split()
        dry_run = "--dry-run" in parts
        scope_parts = [part for part in parts if part != "--dry-run"]
        if not scope_parts:
            selected_scope = await self._select_session_delete_scope()
            if selected_scope is None:
                ui.print("[dim]Deletion cancelled.[/dim]")
                return
            scope = selected_scope
        else:
            scope = scope_parts[0]

        from voidx.memory.cleanup import apply_session_delete_plan, plan_session_delete

        try:
            plan = await plan_session_delete(scope)
        except ValueError as exc:
            ui.error(str(exc))
            return

        self._print_session_delete_plan(plan, dry_run=dry_run)
        if dry_run or not plan.candidates:
            return

        confirmed = await self._confirm_session_delete()
        if not confirmed:
            ui.print("[dim]Deletion cancelled.[/dim]")
            return

        deleted = await apply_session_delete_plan(plan)
        ui.print(f"[green]Deleted {deleted} session(s).[/green]")

    def _print_session_delete_plan(self, plan, *, dry_run: bool) -> None:
        label = "Dry run" if dry_run else "Delete preview"
        ui.print(
            f"[bold]{label}:[/bold] "
            f"{plan.total_sessions} session(s), "
            f"{plan.empty_sessions} empty, "
            f"{plan.sessions_with_messages} with messages, "
            f"{_format_bytes(plan.bytes_to_reclaim)} reclaimable"
        )
        if not plan.candidates:
            ui.print("[dim]No sessions match deletion scope.[/dim]")
            return
        for candidate in plan.candidates:
            title = candidate.title[:50] + ("..." if len(candidate.title) > 50 else "")
            workspace = candidate.workspace[:40] + ("..." if len(candidate.workspace) > 40 else "")
            updated = candidate.updated_at[:10]
            ui.print(
                f"  {candidate.session_id[:8]} | {updated} | {candidate.message_count} msgs | "
                f"{_format_bytes(candidate.bytes_to_reclaim)} | {workspace} | {title}"
            )

    async def _confirm_session_delete(self) -> bool:
        app = self.host.app
        if app is None:
            answer = await self._prompt("Delete these sessions? Type y to confirm", default="")
            return (answer or "").strip().lower() in {"y", "yes"}
        choice = await app.ask_choice(
            "Delete these sessions?",
            [
                ("Cancel", "no", "Keep saved sessions"),
                ("Delete", "yes", "Permanently remove listed sessions"),
            ],
        )
        return choice == "yes"

    async def _select_session_delete_scope(self) -> str | None:
        app = self.host.app
        if app is None:
            return "30d"
        choice = await app.ask_choice(
            "Delete sessions older than:",
            [
                ("7 days", "7d", "Delete sessions older than 7 days"),
                ("15 days", "15d", "Delete sessions older than 15 days"),
                ("30 days", "30d", "Delete sessions older than 30 days"),
                ("All sessions", "all", "Delete every saved session"),
                ("Cancel", "cancel", "Keep saved sessions"),
            ],
        )
        if choice in {None, "cancel"}:
            return None
        return str(choice)

    async def _rollback(self) -> None:
        if not session_tracker.has_rollbackable_changes:
            ui.print("[dim]No file changes to roll back.[/dim]")
            return

        lines = session_tracker.rollback_summary_lines()
        if lines:
            ui.print("[bold]Files changed this turn:[/bold]")
            for line in lines:
                ui.print(line)
            ui.print("")
        ui.print("[yellow]Rollback will overwrite current file contents with the pre-edit snapshot.[/yellow]")

        confirmed = await self._confirm_rollback()
        if not confirmed:
            ui.print("[dim]Rollback cancelled.[/dim]")
            return

        result = session_tracker.rollback_current()
        if result.restored:
            ui.print(f"[green]Restored:[/green] {', '.join(result.restored)}")
        if result.removed:
            ui.print(f"[green]Removed:[/green] {', '.join(result.removed)}")
        if result.ok:
            if not result.restored and not result.removed:
                ui.print("[dim]No files needed rollback.[/dim]")
            return
        for err in result.errors:
            ui.error(err)

    async def _confirm_rollback(self) -> bool:
        app = self.host.app
        if app is None:
            answer = await self._prompt("Rollback these changes? Type y to confirm", default="")
            return (answer or "").strip().lower() in {"y", "yes"}
        choice = await app.ask_choice(
            "Rollback these changes?",
            [
                ("Cancel", "no", "Keep current files"),
                ("Rollback", "yes", "Restore captured snapshots"),
            ],
        )
        return choice == "yes"

    async def _clear(self) -> None:
        await self.host.clear_current_session()
        session_tracker.clear()
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._show_startup(prefer_direct=True)

    async def _list_sessions(self) -> None:
        from voidx.memory.service import list_sessions

        sessions = await list_sessions()
        if not sessions:
            ui.print("No saved sessions.")
            return

        ui.print("[bold]Sessions:[/bold]")
        items = []
        for session in sessions:
            title = session.title[:50] + ("..." if len(session.title) > 50 else "")
            items.append(f"{session.id[:8]} | {title} | {session.workspace} | {getattr(session, 'updated_at', '')[:16]}")

        idx = None
        app = self.host.app
        if app is not None:
            idx = await _select_from_list(app, "Resume session?", items)
        else:
            for item in items:
                ui.print(f"  {item}")

        if idx is not None:
            await self._resume(f"/resume {sessions[idx].id}")

    async def _resume(self, cmd: str) -> None:
        from voidx.memory.service import get_session, list_sessions

        sid = cmd.removeprefix("/resume").strip()
        if not sid:
            sessions = await list_sessions()
            if not sessions:
                ui.print("[dim]No saved sessions.[/dim]")
                return
            items = []
            for session in sessions:
                title = session.title[:50] + ("..." if len(session.title) > 50 else "")
                items.append(f"{session.id[:8]} | {title} | {session.workspace} | {getattr(session, 'updated_at', '')[:16]}")
            idx = None
            app = self.host.app
            if app is not None:
                idx = await _select_from_list(app, "Resume session?", items)
            if idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            sid = sessions[idx].id

        session = await get_session(sid)
        if not session:
            ui.error(f"Session not found: {sid}")
            return

        await self.host.resume_session(session)
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._restore_transcript_snapshot(append=True)
        ui.print(f"[dim]Resumed: {session.id} — {session.title} ({session.message_count} msgs)[/dim]")

    async def _set_title(self, cmd: str) -> None:
        session = self.host.session
        if not session:
            return
        title = cmd.removeprefix("/title").strip()
        if title.lower() == "auto":
            if await self.host.regenerate_session_title():
                ui.print("[dim]Regenerating title...[/dim]")
            else:
                ui.print("[dim]No user message available for title generation.[/dim]")
            return
        if title:
            if await self.host.set_session_title(title):
                ui.print(f"[dim]Title set: {title}[/dim]")

    async def _restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        return await self.host.restore_transcript_snapshot(append=append)

    async def _show_startup(self, **kwargs) -> None:
        await self.host.show_startup(**kwargs)

    async def _skills(self, args: str) -> None:
        parts = args.split(None, 1)
        action = parts[0] if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if action in ("", "list"):
            self._skills_list()
        elif action == "show":
            self._skills_show(target)
        elif action == "enable":
            self._skills_set_enabled(target, True)
        elif action == "disable":
            self._skills_set_enabled(target, False)
        elif action == "auto":
            self._skills_set_auto(target, True)
        elif action == "manual":
            self._skills_set_auto(target, False)
        elif action == "paths":
            self._skills_paths()
        else:
            ui.error("Usage: /skills [list|show|enable|disable|auto|manual|paths]")

    def _skill_service(self) -> SkillService:
        selection = (
            self.host.settings.get_skill_selection()
            if self.host.settings is not None
            else None
        )
        return SkillService(
            SkillRegistry(self.host.workspace),
            selection=selection,
        )

    def _skills_list(self) -> None:
        service = self._skill_service()
        skills = service.list_skills()
        ui.print("[bold]Skills:[/bold]")
        if not skills:
            ui.print("[dim]No skills found. Add SKILL.md files under ~/.voidx/skills or .voidx/skills.[/dim]")
            return
        for skill in skills:
            state = "[green]enabled[/green]" if service.is_enabled(skill) else "[dim]disabled[/dim]"
            mode = "[green]auto[/green]" if service.is_auto(skill) else "[dim]manual[/dim]"
            scope = skill.meta.scope
            desc = f" — {skill.meta.description}" if skill.meta.description else ""
            ui.print(f"  [cyan]{skill.name}[/cyan] · {state} · {mode} · [dim]{scope}[/dim]{desc}")
        ui.print("[dim]Usage: /skills show|enable|disable|auto|manual|paths[/dim]")

    def _skills_show(self, name: str) -> None:
        if not name:
            ui.error("Usage: /skills show <name>")
            return
        service = self._skill_service()
        skill = service.get(name)
        if skill is None:
            ui.error(f"Skill not found: {name}")
            return
        state = "enabled" if service.is_enabled(skill) else "disabled"
        mode = "auto" if service.is_auto(skill) else "manual"
        ui.print(f"[bold]{skill.name}[/bold] [{state}, {mode}]")
        ui.print(f"[dim]{skill.path}[/dim]")
        if skill.meta.description:
            ui.print(skill.meta.description)
        if skill.meta.triggers:
            ui.print(f"[dim]Triggers: {', '.join(skill.meta.triggers)}[/dim]")
        ui.print()
        ui.print(skill.body or "[dim](empty skill body)[/dim]")

    def _skills_set_enabled(self, name: str, enabled: bool) -> None:
        if not name:
            command = "enable" if enabled else "disable"
            ui.error(f"Usage: /skills {command} <name>")
            return
        if self.host.settings is None:
            ui.error("No settings file available.")
            return
        service = self._skill_service()
        if service.get(name) is None:
            ui.error(f"Skill not found: {name}")
            return
        path = self.host.settings.set_skill_enabled(name, enabled)
        self.host.invalidate_skill_service_cache()
        state = "enabled" if enabled else "disabled"
        ui.print(f"[dim]{name} {state}. Saved to {path}[/dim]")

    def _skills_set_auto(self, name: str, auto: bool) -> None:
        if not name:
            command = "auto" if auto else "manual"
            ui.error(f"Usage: /skills {command} <name>")
            return
        if self.host.settings is None:
            ui.error("No settings file available.")
            return
        service = self._skill_service()
        if service.get(name) is None:
            ui.error(f"Skill not found: {name}")
            return
        path = self.host.settings.set_skill_auto(name, auto)
        self.host.invalidate_skill_service_cache()
        mode = "auto" if auto else "manual"
        ui.print(f"[dim]{name} set to {mode}. Saved to {path}[/dim]")

    def _skills_paths(self) -> None:
        registry = SkillRegistry(self.host.workspace)
        ui.print("[bold]Skill paths:[/bold]")
        ui.print(f"  bundled [dim]{registry.bundled_dir}[/dim]")
        ui.print(f"  global  [dim]{registry.global_dir}[/dim]")
        ui.print(f"  project [dim]{registry.project_dir}[/dim]")

    async def _upgrade(self, args: str) -> None:
        action = args.strip().lower() or "check"
        if action == "check":
            await self._upgrade_check()
        elif action == "now":
            await self._upgrade_now()
        elif action == "on":
            self._upgrade_set_enabled(True)
        elif action == "off":
            self._upgrade_set_enabled(False)
        elif action == "status":
            self._upgrade_status()
        else:
            ui.error("Usage: /upgrade [check|now|on|off|status]")

    async def _upgrade_check(self) -> None:
        result = await check_for_update()
        settings = self.host.settings
        mark_update_check = getattr(settings, "mark_update_check", None)
        if callable(mark_update_check):
            mark_update_check(result.latest_version)
        if result.error:
            ui.error(result.message)
            return
        ui.print(result.message)
        if result.update_available:
            ui.print(f"[dim]{upgrade_hint()}[/dim]")

    async def _upgrade_now(self) -> None:
        target = self._cached_upgrade_target()
        if target is not None:
            ui.print(f"[dim]Upgrading to voidx {target}...[/dim]")
            result = await perform_upgrade(target)
        else:
            ui.print("[dim]Checking for updates...[/dim]")
            result = await perform_upgrade()
        if result.ok:
            ui.print(_format_upgrade_success(result))
        else:
            ui.error(result.message)

    def _upgrade_set_enabled(self, enabled: bool) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings file available.")
            return
        path = settings.set_update_check_enabled(enabled)
        state = "enabled" if enabled else "disabled"
        ui.print(f"[dim]Startup update checks {state}. Saved to {path}[/dim]")

    def _upgrade_status(self) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings file available.")
            return
        enabled = "on" if settings.get_update_check_enabled() else "off"
        checked_at = _format_timestamp(settings.get_update_check_last_checked_at())
        latest = settings.get_update_check_latest_version() or "unknown"
        ui.print("[bold]Upgrade checks:[/bold]")
        ui.print(f"  enabled: [cyan]{enabled}[/cyan]")
        ui.print(f"  last checked: [cyan]{checked_at}[/cyan]")
        ui.print(f"  latest seen: [cyan]{latest}[/cyan]")

    def _cached_upgrade_target(self) -> str | None:
        settings = self.host.settings
        if settings is None:
            return None
        update_check_due = getattr(settings, "update_check_due", None)
        if not callable(update_check_due) or update_check_due():
            return None
        get_latest = getattr(settings, "get_update_check_latest_version", None)
        latest = get_latest() if callable(get_latest) else None
        if isinstance(latest, str) and is_newer(latest):
            return latest
        return None

    async def _mcp(self, args: str) -> None:
        parts = args.split(None, 1)
        action = parts[0] if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if action == "new":
            await self._mcp_new()
        elif action == "list":
            await self._mcp_list()
        elif action == "test" or action.startswith("test "):
            await self._mcp_test(target)
        elif action == "del" or action.startswith("del "):
            await self._mcp_del(target)
        elif action == "restart" or action.startswith("restart "):
            await self._mcp_restart(target)
        elif action == "tools" or action.startswith("tools "):
            await self._mcp_tools(target)
        elif action == "disable" or action.startswith("disable "):
            await self._mcp_set_disabled(target, disabled=True)
        elif action == "enable" or action.startswith("enable "):
            await self._mcp_set_disabled(target, disabled=False)
        elif action == "auto" or action.startswith("auto "):
            await self._mcp_set_auto(target, auto=True)
        elif action == "manual" or action.startswith("manual "):
            await self._mcp_set_auto(target, auto=False)
        elif action:
            ui.error("Usage: /mcp [new|list|test|del|restart|tools|disable|enable|auto|manual]")
        else:
            await self._mcp_list()

    async def _mcp_new(self) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings file available.")
            return

        ui.print("[bold]Configure MCP server[/bold]")
        choices = ["voidx-web (built-in)", "Tavily MCP", "URL (SSE / Streamable HTTP)", "Custom command"]
        idx = await _select_from_list(self.host.app, "MCP server type", choices)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return

        web_routes: Mapping[str, WebToolRoute] = {}
        if choices[idx].startswith("voidx-web"):
            server = await self._mcp_builtin_web_config()
            if server is None:
                return
            web_routes = {
                "search": WebToolRoute(backend="mcp", server=server.name, tool="web_search"),
                "fetch": WebToolRoute(backend="mcp", server=server.name, tool="web_fetch"),
            }
        elif choices[idx].startswith("Tavily"):
            server = await self._mcp_tavily_config()
            if server is None:
                return
            web_routes = {
                "search": WebToolRoute(backend="mcp", server=server.name, tool="tavily_search"),
                "fetch": WebToolRoute(backend="mcp", server=server.name, tool="tavily_extract"),
            }
        elif choices[idx].startswith("URL"):
            server = await self._mcp_url_config()
            if server is None:
                return
        else:
            server = await self._mcp_custom_config()
            if server is None:
                return

        ok, tools, err = await self._test_mcp_config(server)
        if not ok:
            ui.error(f"MCP connection failed: {err}")
            ui.print("[dim]Configuration not saved. Check the command and try again.[/dim]")
            return

        tool_names = [tool.name for tool in tools]
        if tool_names:
            server.tools = tool_names

        path = settings.save_mcp_server(server)
        for kind, route in web_routes.items():
            settings.set_web_tool_route(kind, route)

        manager = self.host.mcp_manager
        if manager is not None:
            try:
                await asyncio.wait_for(manager.restart_all(), timeout=30.0)
            except asyncio.TimeoutError:
                ui.warn("MCP restart timed out; servers may still be connecting in the background.")

        ui.print(
            f"  [cyan]{server.name}[/cyan] [green]✓ configured[/green]"
            f" · {len(tool_names)} tool{'s' if len(tool_names) != 1 else ''}"
        )
        if web_routes:
            ui.print("[dim]websearch/webfetch now use this MCP server[/dim]")
        ui.print(f"[dim]Saved to {path}[/dim]")

    async def _mcp_builtin_web_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name", default="voidx-web")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip() or "voidx-web"
        env = {}
        tavily_key = self.host.settings.get_tavily_api_key() if self.host.settings else None
        if tavily_key:
            env["TAVILY_API_KEY"] = tavily_key
        return McpServerConfig(
            name=name,
            command=sys.executable,
            args=["-m", "voidx.mcp.server.web"],
            env=env,
            tools=["web_search", "web_fetch"],
        )

    async def _mcp_tavily_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name", default="tavily")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip() or "tavily"

        env = {}
        env_key = os.environ.get("TAVILY_API_KEY")
        tavily_key = self.host.settings.get_tavily_api_key() if self.host.settings else None
        if not env_key and tavily_key:
            env["TAVILY_API_KEY"] = tavily_key
        elif not env_key:
            tavily_key = await self._prompt("Tavily API key", secret=True)
            if tavily_key is None:
                ui.print("[dim]Cancelled.[/dim]")
                return None
            tavily_key = tavily_key.strip()
            if not tavily_key:
                ui.error("Tavily API key is required.")
                return None
            env["TAVILY_API_KEY"] = tavily_key

        return McpServerConfig(
            name=name,
            command="npx",
            args=["-y", "tavily-mcp@latest"],
            env=env,
            tools=["tavily_search", "tavily_extract"],
        )

    async def _mcp_custom_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip()
        if not name:
            ui.error("Server name is required.")
            return None

        command = await self._prompt("Command")
        if command is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        command = command.strip()
        if not command:
            ui.error("Command is required.")
            return None

        args_text = await self._prompt("Args (shell-style, optional)", default="")
        if args_text is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        try:
            args = shlex.split(args_text)
        except ValueError as exc:
            ui.error(f"Invalid args: {exc}")
            return None

        env_text = await self._prompt("Env VAR=value,VAR2=value2 (optional)", default="")
        if env_text is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        env = _parse_env_pairs(env_text)
        return McpServerConfig(name=name, command=command, args=args, env=env)

    async def _mcp_url_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip()
        if not name:
            ui.error("Server name is required.")
            return None

        transport_choices = ["SSE (legacy)", "Streamable HTTP (MCP 2024-11-05)"]
        t_idx = await _select_from_list(self.host.app, "Transport type", transport_choices)
        if t_idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        transport = "sse" if t_idx == 0 else "streamable-http"

        url_hint = "https://mcp.example.com/sse" if transport == "sse" else "http://127.0.0.1:52222/mcp/"
        url = await self._prompt(f"URL (e.g. {url_hint})")
        if url is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        url = url.strip()
        if not url:
            ui.error("URL is required.")
            return None
        if not url.startswith(("http://", "https://")):
            ui.error("URL must start with http:// or https://")
            return None

        env_text = await self._prompt("Env VAR=value,VAR2=value2 (optional)", default="")
        if env_text is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        env = _parse_env_pairs(env_text)
        return McpServerConfig(name=name, url=url, transport=transport, env=env)

    async def _mcp_list(self) -> None:
        settings = self.host.settings
        if settings is None:
            ui.print("[dim]No settings file available.[/dim]")
            return

        manager = self.host.mcp_manager
        statuses = manager.statuses() if manager is not None and manager.started else []
        ui.print("[bold]MCP servers:[/bold]")
        ui.print(f"[dim]{settings.path}[/dim]")
        if statuses:
            for status in statuses:
                self._print_mcp_status(status)
        else:
            servers = settings.list_mcp_servers()
            if not servers:
                ui.print("[dim]No MCP servers configured. Use /mcp new.[/dim]")
                return
            for server in servers:
                state = "[dim]disabled[/dim]" if server.disabled else "[green]configured[/green]"
                tools = f"{server.tool_count} tool{'s' if server.tool_count != 1 else ''}"
                ui.print(f"  [cyan]{server.name}[/cyan] · {state} · [dim]{tools}[/dim]")

        search = settings.get_web_tool_route("search")
        fetch = settings.get_web_tool_route("fetch")
        if search.backend == "mcp" or fetch.backend == "mcp":
            ui.print()
            ui.print("[bold]Web routing:[/bold]")
            ui.print(f"  search · {search.backend} {search.server}/{search.tool}".rstrip("/"))
            ui.print(f"  fetch  · {fetch.backend} {fetch.server}/{fetch.tool}".rstrip("/"))
        ui.print("[dim]Usage: /mcp new|list|test|del|restart|tools|disable|enable|auto|manual[/dim]")

    async def _mcp_test(self, target: str) -> None:
        async def _do_test(name: str) -> None:
            server = self.host.settings.get_mcp_server(name) if self.host.settings else None
            if server is None:
                ui.error(f"MCP server not found: {name}")
                return
            ok, tools, err = await self._test_mcp_config(server)
            if ok:
                names = ", ".join(tool.name for tool in tools) or "no tools"
                ui.print(f"[green]✓ {name} — connected[/green] [dim]{names}[/dim]")
            else:
                ui.print(f"[red]✗ {name} — {err}[/red]")

        await self._pick_mcp_server("Test", target, _do_test)

    async def _mcp_del(self, target: str) -> None:
        async def _do_delete(name: str) -> None:
            if self.host.settings is None:
                ui.error("No settings file available.")
                return
            path = self.host.settings.delete_mcp_server(name)
            manager = self.host.mcp_manager
            if manager is not None:
                try:
                    await asyncio.wait_for(manager.restart_all(), timeout=30.0)
                except asyncio.TimeoutError:
                    ui.warn("MCP restart timed out after deletion; servers may still be reconnecting.")
            ui.print(f"[dim]'{name}' removed.[/dim]")
            ui.print(f"[dim]Cleaned {path}[/dim]")

        await self._pick_mcp_server("Delete", target, _do_delete)

    async def _mcp_restart(self, target: str) -> None:
        _ = target
        manager = self.host.mcp_manager
        if manager is None:
            ui.error("No MCP manager available.")
            return
        try:
            await asyncio.wait_for(manager.restart_all(), timeout=30.0)
        except asyncio.TimeoutError:
            ui.warn("MCP restart timed out; servers may still be connecting in the background.")
            return
        ui.print("[green]✓ MCP servers restarted[/green]")

    async def _mcp_set_disabled(self, target: str, *, disabled: bool) -> None:
        action = "Disable" if disabled else "Enable"

        async def _do_set(name: str) -> None:
            if self.host.settings is None:
                ui.error("No settings file available.")
                return
            try:
                path = self.host.settings.set_mcp_server_disabled(name, disabled)
            except KeyError:
                ui.error(f"MCP server not found: {name}")
                return
            manager = self.host.mcp_manager
            if manager is not None:
                try:
                    await asyncio.wait_for(manager.restart_all(), timeout=30.0)
                except asyncio.TimeoutError:
                    ui.warn("MCP restart timed out; servers may still be reconnecting.")
            state = "disabled" if disabled else "enabled"
            ui.print(f"[green]✓ {name} {state}[/green]")
            if disabled:
                ui.print("[dim]Web routes pointing to this server were cleared.[/dim]")
            ui.print(f"[dim]Saved to {path}[/dim]")

        await self._pick_mcp_server(action, target, _do_set)

    async def _mcp_set_auto(self, target: str, *, auto: bool) -> None:
        action = "Auto-discovery" if auto else "Manual-only"

        async def _do_set(name: str) -> None:
            if self.host.settings is None:
                ui.error("No settings file available.")
                return
            try:
                path = self.host.settings.set_mcp_server_auto(name, auto)
            except KeyError:
                ui.error(f"MCP server not found: {name}")
                return
            mode = "auto-discovery" if auto else "manual-only"
            ui.print(f"[green]✓ {name} set to {mode}[/green]")
            ui.print(f"[dim]Saved to {path}[/dim]")
            if auto:
                ui.print("[dim]Restart the session for the system prompt to include this server.[/dim]")

        await self._pick_mcp_server(action, target, _do_set)

    async def _mcp_tools(self, target: str) -> None:
        async def _do_tools(name: str) -> None:
            manager = self.host.mcp_manager
            if manager is None:
                ui.error("No MCP manager available.")
                return
            try:
                tools = await asyncio.wait_for(
                    manager.list_tools_for_server(name), timeout=15.0,
                )
            except asyncio.TimeoutError:
                ui.error(f"Listing tools for {name} timed out.")
                return
            except Exception as exc:
                ui.error(f"Could not list tools for {name}: {exc}")
                return
            ui.print(f"[bold]{name} tools:[/bold]")
            if not tools:
                ui.print("[dim]No tools.[/dim]")
                return
            for tool in tools:
                ui.print(f"  [cyan]{tool.name}[/cyan] — {tool.description or '(no description)'}")

        await self._pick_mcp_server("Tools", target, _do_tools)

    async def _pick_mcp_server(self, action: str, target: str, callback) -> None:
        if self.host.settings is None:
            ui.error("No settings file available.")
            return
        if target:
            await callback(target)
            return
        names = [server.name for server in self.host.settings.list_mcp_servers()]
        if not names:
            ui.print("[yellow]No MCP servers configured. Use /mcp new first.[/yellow]")
            return
        ui.print(f"[bold]{action}[/bold] — select MCP server:")
        idx = await _select_from_list(self.host.app, action, names)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        await callback(names[idx])

    @staticmethod
    async def _test_mcp_config(server: McpServerConfig, timeout: float = 30.0):
        from voidx.mcp.client import McpClient

        client = McpClient(server)
        try:
            await asyncio.wait_for(client.start(), timeout=timeout)
            tools = await asyncio.wait_for(client.list_tools(), timeout=timeout)
            return True, tools, ""
        except asyncio.TimeoutError:
            return False, [], f"connection timed out after {timeout:.0f}s"
        except Exception as exc:
            return False, [], str(exc)
        finally:
            try:
                await asyncio.wait_for(client.stop(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    @staticmethod
    def _print_mcp_status(status) -> None:
        if status.status == "connected":
            state = "[green]connected[/green]"
        elif status.status == "connecting":
            state = "[yellow]connecting…[/yellow]"
        elif status.status == "error":
            state = "[red]error[/red]"
        elif status.status == "disabled":
            state = "[dim]disabled[/dim]"
        else:
            state = "[yellow]disconnected[/yellow]"
        tools = f"{status.tool_count} tool{'s' if status.tool_count != 1 else ''}" if status.tool_count else ""
        err = f" · [dim]{status.error_message}[/dim]" if status.error_message else ""
        ui.print(f"  [cyan]{status.name}[/cyan] · {state}{f' · {tools}' if tools else ''}{err}")

    async def _loop(self, args: str) -> None:
        manager = getattr(self.host, "loop_manager", None)
        if manager is None:
            ui.error("/loop is not available in this session.")
            return

        arg = args.strip()
        if not arg or arg == "help":
            ui.print("[dim]Usage: /loop [interval] <prompt>, /loop stop, /loop status[/dim]")
            return
        if arg == "stop":
            manager.stop()
            ui.print("[dim]/loop stopped.[/dim]")
            return
        if arg == "status":
            status = manager.status()
            if status is None:
                ui.print("[dim]No active /loop.[/dim]")
            else:
                ui.print(f"[dim]/loop active: {status}[/dim]")
            return

        interval_seconds, prompt = _parse_interval(arg)
        if not prompt.strip():
            ui.error("/loop requires a prompt.")
            return
        session = getattr(self.host, "session", None)
        ctx = _tool_context_for_host(self.host)
        manager.start(
            PromptSource.from_raw(prompt.strip()),
            interval_seconds,
            bash_tool=BashTool(),
            ctx=ctx,
            session_id=getattr(session, "id", None),
        )
        mode = "dynamic" if interval_seconds is None else f"every {int(interval_seconds)}s"
        ui.print(f"[dim]/loop started ({mode}).[/dim]")

    async def _lang(self, args: str) -> None:
        value = args.strip()
        if not value:
            await self._lang_interactive()
            return
        self._apply_language(value)

    async def _tone(self, args: str) -> None:
        value = args.strip()
        if not value:
            await self._tone_interactive()
            return
        self._apply_tone(value)

    async def _lang_interactive(self) -> None:
        from voidx.agent.prompts import _LANGUAGE_LABELS

        items: list[str] = []
        values: list[str] = []
        for _key, (name, tag) in _LANGUAGE_LABELS.items():
            items.append(f"{name} [{tag}]")
            values.append(tag)
        if self.host.app is None:
            await self._lang_headless(values)
            return
        selected = await self._pick_or_reset(
            "Language",
            items,
            values,
            "Language code (e.g. fr, de, pt-BR; auto to reset)",
            "Other (enter manually)",
            "Reset (auto-detect)",
        )
        if selected is not None:
            self._apply_language(selected)

    async def _tone_interactive(self) -> None:
        from voidx.agent.runtime_context import _TONE_LABELS

        items: list[str] = []
        values: list[str] = []
        for value, (name, description, _instruction) in _TONE_LABELS.items():
            items.append(f"{name} - {description}")
            values.append(value)
        if self.host.app is None:
            await self._tone_headless(values)
            return
        selected = await self._pick_or_reset(
            "Tone",
            items,
            values,
            "Tone (e.g. patient, enthusiastic; default to reset)",
            "Other (enter manually)",
            "Reset (default)",
        )
        if selected is not None:
            self._apply_tone(selected)

    async def _pick_or_reset(
        self,
        title: str,
        option_items: list[str],
        values: list[str],
        prompt_label: str,
        other_label: str,
        reset_label: str,
    ) -> str | None:
        items = [*option_items, other_label, reset_label]
        idx = await _select_from_list(self.host.app, title, items)
        if idx is None or idx < 0 or idx >= len(items):
            ui.print("[dim]Cancelled.[/dim]")
            return None
        if idx == len(values):
            result = await self._prompt(prompt_label)
            if result is None or not result.strip():
                ui.print("[dim]Cancelled.[/dim]")
                return None
            return result.strip()
        if idx == len(values) + 1:
            return ""
        return values[idx]

    async def _lang_headless(self, values: list[str]) -> None:
        ui.print(f"Language: [cyan]{self._current_language_label()}[/cyan]")
        ui.print(f"[dim]Available: {', '.join(values)}[/dim]")
        value = await self._prompt("Language code (or 'auto' to reset)", default="")
        if value is None or not value.strip():
            ui.print("[dim]Cancelled.[/dim]")
            return
        self._apply_language(value)

    async def _tone_headless(self, values: list[str]) -> None:
        ui.print(f"Tone: [cyan]{self._current_tone_label()}[/cyan]")
        ui.print(f"[dim]Available: {', '.join(values)}[/dim]")
        value = await self._prompt("Tone (or 'default' to reset)", default="")
        if value is None or not value.strip():
            ui.print("[dim]Cancelled.[/dim]")
            return
        self._apply_tone(value)

    def _apply_language(self, value: str) -> None:
        settings = self.host.settings
        if settings is not None:
            settings.set_user_language(value)
            profile = settings.get_user_profile()
        else:
            profile = self._current_user_profile()
            profile.language = _normalize_language(value)
        self._set_current_user_profile(profile)
        ui.print(f"Language: [cyan]{profile.language or 'auto-detect'}[/cyan] [green]✓[/green]")

    def _apply_tone(self, value: str) -> None:
        settings = self.host.settings
        if settings is not None:
            settings.set_user_tone(value)
            profile = settings.get_user_profile()
        else:
            profile = self._current_user_profile()
            profile.tone = _normalize_tone(value)
        self._set_current_user_profile(profile)
        ui.print(f"Tone: [cyan]{profile.tone or 'default'}[/cyan] [green]✓[/green]")

    def _current_user_profile(self) -> UserProfile:
        profile = getattr(self.host.config, "user_profile", None)
        if isinstance(profile, UserProfile):
            return profile.model_copy()
        return UserProfile()

    def _set_current_user_profile(self, profile: UserProfile) -> None:
        self.host.config.user_profile = profile

    def _current_language_label(self) -> str:
        profile = self._current_user_profile()
        return profile.language or "auto"

    def _current_tone_label(self) -> str:
        profile = self._current_user_profile()
        return profile.tone or "default"

    async def _model_new(self) -> None:
        """Interactive model configuration — create or update a named profile."""
        from voidx.config import Profile
        from voidx.llm.service import create_chat_model

        ui.print("[bold]Configure LLM[/bold]")

        # Step 1: choose provider via arrow keys
        providers = await get_providers(self.host.settings)
        provider_choices = providers + ["Add custom provider..."]
        idx = await _select_from_list(self.host.app, "Provider", provider_choices)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if provider_choices[idx] == "Add custom provider...":
            new_provider = await self._prompt("Provider name")
            if not new_provider or not new_provider.strip():
                ui.error("Provider name is required.")
                return
            new_provider = new_provider.strip()
            protocol_choices = ["openai", "anthropic", "gemini", "deepseek"]
            proto_idx = await _select_from_list(self.host.app, "Protocol", protocol_choices)
            if proto_idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            protocol = protocol_choices[proto_idx]
            if protocol == "deepseek":
                ui.print("[dim]  deepseek: China-domestic OpenAI-compatible providers (DeepSeek, Qwen, Zhipu, etc.)[/dim]")
            ui.print(f"[dim]  Custom provider: {new_provider} (protocol={protocol})[/dim]")
        else:
            new_provider = provider_choices[idx]
            protocol = (await self.host.settings.resolve_protocol(new_provider)) if self.host.settings else None
        ui.print(f"[dim]  Provider: {new_provider}[/dim]")

        # Step 2: connection details, used immediately for model discovery
        current_base_url = ""
        current_key = ""
        if self.host.settings:
            current_base_url = await self.host.settings.resolve_base_url(new_provider) or ""
            current_key = await self.host.settings.resolve_api_key(new_provider) or ""

        base_url_input = await self._prompt("Base URL (optional)", default=current_base_url)
        if base_url_input is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        base_url = base_url_input.strip() or current_base_url or None

        masked = self._mask_key(current_key) if current_key else "(not set)"
        ui.print(f"[dim]Current: {masked}[/dim]")
        new_key = await self._prompt("API key", default="", secret=True)
        if new_key is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if new_key.strip():
            api_key = new_key.strip()
        else:
            if not current_key:
                ui.error(
                    f"No API key found for '{new_provider}'. Provide one now."
                )
                return
            api_key = current_key

        # Step 3: choose model from fetched list or enter manually
        from voidx.llm.catalog import (
            list_fallback_models,
            list_models_for_config,
        )
        try:
            known = await asyncio.wait_for(
                list_models_for_config(
                    new_provider,
                    api_key=api_key,
                    base_url=base_url,
                    protocol=protocol,
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            known = await list_fallback_models(new_provider, protocol=protocol)
            ui.warn("Model list fetch timed out; using saved/static model list.")
        model_choices = known + ["Other (enter manually)"]
        ui.print()
        model_idx = await _select_from_list(self.host.app, "Model", model_choices)
        if model_idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if model_choices[model_idx] == "Other (enter manually)":
            new_model = await self._prompt(
                f"Model name",
                default=self.host.config.model.model,
            )
            if new_model is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            if not new_model.strip():
                ui.error("Model name is required.")
                return
            new_model = new_model.strip()
        else:
            new_model = model_choices[model_idx]
            ui.print(f"[dim]  Model: {new_model}[/dim]")

        # Step 4: build and validate
        test_cfg = self.host.config.model.model_copy()
        test_cfg.provider = new_provider
        test_cfg.model = new_model
        test_cfg.base_url = base_url
        test_cfg.protocol = protocol

        test_model = create_chat_model(api_key, test_cfg)

        ui.print()
        ui.print(f"[dim]  Testing connection to {new_provider}/{new_model}...[/dim]")

        ok, err_msg = await self._test_connection(test_model)
        if not ok:
            ui.error(f"Connection failed: {err_msg}")
            ui.print("[dim]Configuration not saved. Check your API key and try again.[/dim]")
            return

        # Step 5: save profile (key = provider/model) and activate
        profile_key = f"{new_provider}/{new_model}"
        profile = Profile(
            name=profile_key,
            api_key=api_key,
            base_url=base_url,
            protocol=protocol,
        )
        env_path = await self.host.settings.save_profile(profile)

        self.host.config.model.provider = new_provider
        self.host.config.model.model = new_model
        self.host.config.model.base_url = base_url
        self.host.config.model.protocol = protocol
        self._sync_context_limit()
        self.host.api_key = api_key
        self.host.model = test_model

        ui.print(f"  [cyan]{profile_key}[/cyan] [green]✓ configured[/green]")
        ui.print(f"[dim]Saved to {env_path}[/dim]")
        await self._show_startup(prefer_direct=True)

    @staticmethod
    async def _test_connection(model, timeout: float = 30.0) -> tuple[bool, str]:
        """Test an LLM connection with a minimal prompt. Returns (ok, error_msg)."""
        from langchain_core.messages import HumanMessage
        try:
            resp = await asyncio.wait_for(
                model.ainvoke([HumanMessage(content="hi")]),
                timeout=timeout,
            )
            if resp and getattr(resp, "content", None):
                return True, ""
            return False, "empty response"
        except asyncio.TimeoutError:
            return False, f"timed out after {timeout:.0f}s"
        except Exception as e:
            msg = str(e)
            # Extract the most useful part of the error
            if len(msg) > 300:
                msg = msg[:300] + "..."
            return False, msg

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "****" + key[-4:]

    async def _list_models(self) -> None:
        from voidx.llm.catalog import list_models

        current = f"{self.host.config.model.provider}/{self.host.config.model.model}"
        ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]\n")

        for provider in await get_providers(self.host.settings):
            ui.print(f"  [bold]{provider}[/bold] ", end="")
            try:
                models = await asyncio.wait_for(list_models(provider), timeout=15.0)
            except asyncio.TimeoutError:
                models = []
                ui.print("[dim](fetch timed out)[/dim]")
                continue
            if models:
                shown = models[:8]
                suffix = f" [dim](+{len(models) - 8} more)[/dim]" if len(models) > 8 else ""
                ui.print(f"{'  '.join(shown)}{suffix}")
            else:
                ui.print("[dim](none)[/dim]")
        ui.print()
        ui.print("[dim]Usage: /model list|new|reasoning|ctx|test|del|switch|<name>[/dim]")

    async def _model_list(self) -> None:
        cfg = self.host.config
        if self.host.settings is None:
            ui.error("No Settings reference.")
            return

        current = f"{cfg.model.provider}/{cfg.model.model}"
        ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]")

        profiles = await self.host.settings.list_profiles()
        if not profiles:
            ui.print("[dim]No profiles configured. Use /model new.[/dim]")
            return

        ui.print()
        for p in profiles:
            is_active = p.name == current
            marker = " *" if is_active else "  "
            masked = self._mask_key(p.api_key) if p.api_key else "(env)"
            ui.print(f" {marker} [cyan]{p.name}[/cyan] {masked}")

    async def _profile_names(self) -> list[str]:
        """Return names of configured profiles."""
        if self.host.settings is None:
            return []
        return [p.name for p in await self.host.settings.list_profiles()]

    async def _pick_or_act(self, action: str, target: str, callback) -> None:
        """If *target* is a profile name, call callback(target).
        Otherwise show profiles for arrow-key selection, then call callback."""
        import sys as _sys

        if target:
            await callback(target)
            _sys.stdout.flush()
            return

        names = await self._profile_names()
        if not names:
            ui.print("[yellow]No profiles configured. Use /model new first.[/yellow]")
            return

        ui.print(f"[bold]{action}[/bold] — select profile (↑↓ Enter, ESC cancel):")
        idx = await _select_from_list(self.host.app, action, names)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        await callback(names[idx])
        _sys.stdout.flush()

    async def _model_test(self, target: str) -> None:
        async def _do_test(profile_name: str) -> None:
            from voidx.llm.service import create_chat_model
            settings = self.host.settings
            if settings is None:
                ui.error("No Settings reference.")
                return
            profile = await settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            cfg = self.host.config.model.model_copy()
            cfg.provider = profile.provider
            cfg.model = profile.model
            cfg.base_url = profile.base_url or await settings.resolve_base_url(profile.provider)
            cfg.protocol = profile.protocol or await settings.resolve_protocol(profile.provider)
            model = create_chat_model(profile.api_key, cfg)
            ui.print(f"[dim]Testing {profile.name} ({profile.provider}/{profile.model})...[/dim]")
            ok, err_msg = await self._test_connection(model)
            if ok:
                ui.print(f"[green]✓ {profile.name} — connection successful[/green]")
            else:
                ui.print(f"[red]✗ {profile.name} — {err_msg}[/red]")

        await self._pick_or_act("Test", target, _do_test)

    async def _model_del(self, target: str) -> None:
        async def _do_delete(profile_name: str) -> None:
            if self.host.settings is None:
                ui.error("No Settings reference.")
                return
            profile = await self.host.settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            env_path = await self.host.settings.delete_profile(profile_name)
            was_active = (self.host.config.model.provider == profile.provider
                          and self.host.config.model.model == profile.model)
            if was_active:
                self.host.model = None
                self.host.api_key = None
                ui.print(f"[yellow]'{profile_name}' removed. Model disconnected.[/yellow]")
            else:
                ui.print(f"[dim]'{profile_name}' removed.[/dim]")
            ui.print(f"[dim]Cleaned {env_path}[/dim]")

        await self._pick_or_act("Delete", target, _do_delete)

    async def _model_switch(self, target: str) -> None:
        from voidx.config import Profile
        from voidx.llm.service import create_chat_model
        from voidx.memory.service import update_session_model

        target, scope = self._model_switch_scope(target)

        async def _do_switch(profile_name: str) -> None:
            settings = self.host.settings
            if settings is None:
                ui.error("No Settings reference.")
                return
            profile = await settings.resolve_profile(profile_name)
            if profile is None:
                if "/" in profile_name:
                    new_provider, new_model = profile_name.split("/", 1)
                    new_provider = new_provider.lower()
                elif " " in profile_name:
                    parts = profile_name.split(None, 1)
                    new_provider = parts[0].lower()
                    new_model = parts[1]
                else:
                    new_provider = self.host.config.model.provider
                    new_model = profile_name
                new_key = await settings.resolve_api_key(new_provider)
                if not new_key:
                    ui.error(f"No API key found for '{new_provider}'. Use /model new.")
                    return
                profile = Profile(
                    name=f"{new_provider}/{new_model}",
                    api_key=new_key,
                    base_url=await settings.resolve_base_url(new_provider),
                    protocol=await settings.resolve_protocol(new_provider),
                )
            self.host.config.model.provider = profile.provider
            self.host.config.model.model = profile.model
            self.host.config.model.base_url = profile.base_url or await settings.resolve_base_url(profile.provider)
            self.host.config.model.protocol = profile.protocol or await settings.resolve_protocol(profile.provider)
            self._sync_context_limit()
            self.host.api_key = profile.api_key
            self.host.model = create_chat_model(profile.api_key, self.host.config.model)
            await settings.save_profile(profile, scope=scope)
            if self.host.session:
                await update_session_model(self.host.session.id, profile.provider, profile.model)
            scope_label = "global + local" if scope == "global" else "local"
            ui.print(f"[cyan]{profile.name}[/cyan] ({profile.provider}/{profile.model}) [green]✓ switched ({scope_label})[/green]")

        await self._pick_or_act("Switch", target, _do_switch)

    async def _model_reasoning(self, effort: str) -> None:
        valid = ("off", "low", "medium", "high", "xhigh")

        if effort and effort in valid:
            new_effort = effort
        elif not effort:
            current = self.host.config.model.reasoning_effort or "xhigh"
            choices = list(valid)
            idx = await _select_from_list(self.host.app, "Select effort", choices)
            if idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            new_effort = choices[idx]
        else:
            ui.error(f"Invalid effort: '{effort}'. Use: {', '.join(valid)}")
            return

        self.host.config.model.reasoning_effort = new_effort
        self._sync_context_limit()

        if self.host.api_key:
            from voidx.llm.service import create_chat_model
            self.host.model = create_chat_model(self.host.api_key, self.host.config.model)

        ui.print(f"Reasoning effort: [cyan]{new_effort}[/cyan] [green]✓[/green]")

    async def _model_ctx(self, target: str) -> None:
        choices_map: dict[str, int | None] = {
            "128k": 128_000,
            "256k": 256_000,
            "384k": 384_000,
            "512k": 512_000,
            "1M": 1_000_000,
            "Auto": None,
        }

        if target:
            key = target.lower()
            normalized = {c.lower(): (c, v) for c, v in choices_map.items()}
            if key not in normalized:
                ui.error(f"Invalid context window: '{target}'. Use: {', '.join(choices_map)}")
                return
            new_label, new_value = normalized[key]
        else:
            choices = list(choices_map)
            idx = await _select_from_list(self.host.app, "Context window", choices)
            if idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            new_value = choices_map[choices[idx]]
            new_label = choices[idx]

        self.host.config.model.context_window = new_value
        self._sync_context_limit()

        settings = self.host.settings
        if settings is not None:
            if new_value is None:
                settings._pop_setting("context_window")
            else:
                settings._set_setting("context_window", new_value)

        display = "Auto (provider default)" if new_value is None else f"{new_label}"
        ui.print(f"Context window: [cyan]{display}[/cyan] [green]✓[/green]")

    @staticmethod
    def _model_switch_scope(raw: str) -> tuple[str, str]:
        scope = "local"
        filtered: list[str] = []
        for token in raw.strip().split():
            if token == "--local":
                scope = "local"
            elif token == "--global":
                scope = "global"
            else:
                filtered.append(token)
        return " ".join(filtered), scope

    def _sync_context_limit(self) -> None:
        from voidx.llm.service import get_context_limit

        limit = get_context_limit(self.host.config.model.provider, self.host.config.model.protocol or "", self.host.config.model.context_window)
        stats = self.host.usage_stats
        if stats is not None:
            stats.context_limit = limit
        app = self.host.app
        if app is not None:
            app.status.context_limit = limit
            app.status.provider = self.host.config.model.provider
            app.status.model = self.host.config.model.model
            app.status.reasoning_effort = self.host.config.model.reasoning_effort or "xhigh"
