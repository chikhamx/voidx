"""Tool permission helpers for the agent graph."""

from __future__ import annotations

import re

from voidx.agent.graph_parts.runtime import ui
from voidx.ui.events import PermissionToolDetail


class GraphPermissionMixin:
    async def _authorize_tool_calls(
        self,
        tool_calls: list[dict],
        agent_name: str,
        plan_mode: bool,
        session_id: str,
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        plan_denied_tools = {"write", "edit", "bash"}
        approved: list[dict] = []
        denied: list[tuple[dict, str]] = []
        need_ask: list[dict] = []

        for tc in tool_calls:
            tid = self._repair_tool_name(tc.get("name", ""))
            repaired = {**tc, "name": tid}
            targs = repaired.get("args", {})
            pattern = self._build_pattern(tid, targs)

            if plan_mode and tid in plan_denied_tools:
                denied.append((repaired, f"BLOCKED by plan mode: '{tid}' is not allowed."))
                continue
            if plan_mode and tid == "task" and targs.get("subagent_type") == "implement":
                denied.append((repaired, "BLOCKED by plan mode: cannot delegate to implement."))
                continue
            action = self._permission.decide(tid, pattern)
            if action == "allow":
                approved.append(repaired)
            elif action == "deny":
                denied.append((repaired, f"Permission denied: {tid} → {pattern}"))
            elif tid == "bash" and self._is_safe_bash(targs.get("command", "")):
                approved.append(repaired)
            else:
                need_ask.append(repaired)

        if need_ask:
            choice = await self._ask_tool_permission(need_ask)
            if choice is None:
                choice = "n"

            if choice == "a":
                for tc in need_ask:
                    self._permission.allow_silent(tc["name"])
                self._notice_permission_result(f"{len(need_ask)} tools allowed for this session")
                approved.extend(need_ask)
            elif choice == "y":
                self._notice_permission_result(f"{len(need_ask)} tools allowed once")
                approved.extend(need_ask)
            else:
                self._notice_permission_result(f"{len(need_ask)} tools denied")
                for tc in need_ask:
                    denied.append((tc, f"User denied: {tc['name']}"))

        return approved, denied

    async def _ask_tool_permission(self, tool_calls: list[dict]) -> str | None:
        tool_list = ", ".join(t["name"] for t in tool_calls)
        choices = [
            ("Yes, always", "a", "Allow these tools for this session"),
            ("Yes", "y", "Allow this tool use once"),
            ("No", "n", "Deny these tools"),
        ]
        details = [item.model_dump() for item in self._permission_tool_details(tool_calls)]

        if not self._app:
            ui.print("")
            ui.print(f"  [yellow]Allow tools: [bold]{tool_list}[/bold]?[/yellow]")

        if self._app:
            return await self._app.ask_choice("Allow tool use?", choices, details=details)
        return "n"

    def _notice_permission_result(self, message: str) -> None:
        if self._app:
            self._app.set_notice(message)
            return
        ui.print(f"[dim]✓ {message}[/dim]")

    @staticmethod
    def _repair_tool_name(tid: str) -> str:
        """Auto-repair common LLM tool name mistakes.
        Claude Code has experimental_repairToolCall for this."""
        tool_map = {
            # PascalCase → snake_case
            "Read": "read", "Write": "write", "Edit": "edit",
            "MultiEdit": "edit", "multiEdit": "edit", "multi_edit": "edit",
            "Glob": "glob", "Grep": "grep", "Bash": "bash",
            "Task": "task", "TodoWrite": "todo", "Todo": "todo",
            "WebFetch": "webfetch", "WebSearch": "websearch",
            # Legacy names
            "read_file": "read", "write_file": "write",
            "edit_file": "edit", "shell": "bash",
            # Misc
            "readfile": "read", "writefile": "write",
            "search": "grep", "find": "glob",
            "RepoMap": "repo_map", "repomap": "repo_map", "Repo_map": "repo_map",
        }
        return tool_map.get(tid, tool_map.get(tid.lower(), tid))

    @staticmethod
    def _build_pattern(tool: str, args: dict) -> str:
        """Build a permission pattern from tool args.
        For bash: use the command string.
        For file tools: use the file path.
        Default: "*"
        """
        if tool == "bash":
            return args.get("command", "*")
        if tool in ("read", "write", "edit"):
            return args.get("file_path", "*")
        if tool == "task":
            return args.get("subagent_type", "*")
        return "*"

    def _permission_tool_details(self, tool_calls: list[dict]) -> list[PermissionToolDetail]:
        details: list[PermissionToolDetail] = []
        for call in tool_calls:
            name = str(call.get("name", ""))
            args = call.get("args", {})
            if not isinstance(args, dict):
                args = {}
            details.append(PermissionToolDetail(
                name=name,
                pattern=self._build_pattern(name, args),
                args=args,
            ))
        return details

    @staticmethod
    def _is_safe_bash(command: str) -> bool:
        """Check if a bash command is read-only (safe to auto-allow)."""
        stripped = command.strip()
        if not stripped or stripped.startswith("#"):
            return True
        # Redirection to file → write
        if re.search(r" > ", stripped) or re.search(r" >> ", stripped):
            return False
        if re.search(r"\|\s*tee\b", stripped):
            return False

        # Parse first program word (skip leading env VAR=val assignments)
        words = stripped.split()
        prog = words[0].lower()

        # ── git ──────────────────────────────────────────────────────
        if prog == "git" and len(words) > 1:
            sub = words[1]
            READ_ONLY_GIT = {
                "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
                "ls-files", "ls-tree", "describe", "shortlog", "reflog", "cherry",
                "whatchanged", "notes", "grep", "bisect",
                "config", "stash", "branch", "tag", "remote", "worktree",
            }
            if sub not in READ_ONLY_GIT:
                return False
            if sub == "stash":
                return len(words) > 2 and words[2] in ("list", "show")
            if sub == "bisect":
                return len(words) > 2 and words[2] in ("log", "view", "visualize")
            if sub in ("branch", "tag"):
                return "-d" not in words and "-D" not in words
            if sub == "remote":
                return "-v" in words or "--verbose" in words or len(words) == 2
            if sub == "worktree":
                return len(words) > 2 and words[2] == "list"
            return True

        # ── gh CLI ───────────────────────────────────────────────────
        if prog == "gh" and len(words) > 1:
            sub = words[1]
            if sub == "pr":
                return len(words) > 2 and words[2] in ("view", "list", "status", "checks", "diff")
            if sub == "issue":
                return len(words) > 2 and words[2] in ("view", "list", "status")
            if sub == "api":
                cmd_upper = stripped.upper()
                if "-X" in cmd_upper or "--method" in cmd_upper:
                    return "GET" in cmd_upper
                return True
            if sub in ("auth", "config", "completion", "secret"):
                return len(words) == 2 or (len(words) > 2 and words[2] in ("list", "status", "view"))
            return False

        # ── read-only shell commands ─────────────────────────────────
        READ_ONLY = {
            "ls", "dir", "cat", "head", "tail", "wc", "which", "where", "whereis",
            "echo", "printf", "pwd", "date", "whoami", "uname", "env", "printenv",
            "df", "du", "sort", "uniq", "cut", "tr", "column", "less", "more",
            "find", "grep", "egrep", "fgrep", "rg", "file", "stat", "od",
            "true", "false", "test", "[", "type", "basename", "dirname",
            "realpath", "readlink", "hostname", "id", "groups", "logname",
            "uptime", "free", "swapon", "lscpu", "lsblk", "lspci", "lsusb",
        }
        if prog in READ_ONLY:
            return True

        # ── package managers — read-only subcommands only ────────────
        if prog in ("pip", "pip3") and len(words) > 1:
            return words[1] in ("list", "show", "freeze", "config", "cache")
        if prog in ("npm", "npx") and len(words) > 1:
            return words[1] in ("list", "ls", "view", "info", "outdated")
        if prog == "cargo" and len(words) > 1:
            return words[1] in ("search", "doc", "readme")
        if prog == "go" and len(words) > 1:
            return words[1] in ("list", "doc", "version", "env")

        return False
