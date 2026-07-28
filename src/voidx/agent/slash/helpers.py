"""Shared formatting helpers for slash commands."""
from __future__ import annotations

import re
import time
from voidx.config import CodeIde
from voidx.selfupdate import UpgradeResult
from voidx.tools.service import ToolContext

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
