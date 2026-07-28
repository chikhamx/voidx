"""Secret redaction helpers for AI approval payloads."""

from __future__ import annotations

import hashlib
import json
import re


_SENSITIVE_KEYS = {"api_key", "authorization", "cookie", "password", "secret", "token"}
_SHELL_VALUE = r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s;&|]+)'''
_SENSITIVE_HEADER_NAME = (
    r"(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"[a-z0-9-]*(?:api[-_]?key|token|secret|password)[a-z0-9-]*)"
)
_SENSITIVE_QUOTED_HEADER_RE = re.compile(
    rf"(?i)(?P<header_quote>['\"])(?P<header_prefix>{_SENSITIVE_HEADER_NAME}\s*:\s*)"
    rf"(?:bearer\s+|basic\s+)?[^'\"\r\n]*(?P=header_quote)"
)
_SENSITIVE_HEADER_RE = re.compile(
    rf"(?i)(?<!['\"])\b({_SENSITIVE_HEADER_NAME}\s*:\s*)"
    rf"(?:bearer\s+|basic\s+)?[^\s;&|'\"]+"
)
_SENSITIVE_ENV_RE = re.compile(
    rf"(?i)(?<![a-z0-9_])((?:\$env:)?(?P<key_quote>['\"]?)"
    rf"(?=[a-z0-9_]*(?:api_?key|token|secret|password|cookie|authorization))"
    rf"[a-z_][a-z0-9_]*(?P=key_quote)\s*=\s*){_SHELL_VALUE}"
)
_SENSITIVE_FLAG_RE = re.compile(
    rf"(?i)(?<!\S)(--?[a-z0-9_-]*(?:api[-_]?key|token|secret|password|cookie|authorization)"
    rf"[a-z0-9_-]*|-u|--user)(=|\s+){_SHELL_VALUE}"
)
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^/\s@]+)@", re.IGNORECASE)
_SSHPASS_PASSWORD_RE = re.compile(rf"(?i)(\bsshpass\s+-p(?:=|\s+)){_SHELL_VALUE}")
_PACKAGE_CONFIG_SECRET_RE = re.compile(
    rf"(?i)(\b(?:npm|pnpm|yarn)\s+config\s+set\s+\S*(?:auth|token|password|secret)\S*\s+)"
    rf"{_SHELL_VALUE}"
)



def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or any(part in lowered for part in _SENSITIVE_KEYS)


def _redact(value):
    if isinstance(value, dict):
        return {str(key): "<redacted>" if _is_sensitive(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value




def _redact_shell_command(command: str) -> tuple[str, bool]:
    redacted = _URL_USERINFO_RE.sub(r"\1<redacted>@", command)
    redacted = _SENSITIVE_QUOTED_HEADER_RE.sub(
        lambda match: (
            f"{match.group('header_quote')}{match.group('header_prefix')}"
            f"<redacted>{match.group('header_quote')}"
        ),
        redacted,
    )
    redacted = _SENSITIVE_HEADER_RE.sub(r"\1<redacted>", redacted)
    redacted = _SENSITIVE_ENV_RE.sub(r"\1<redacted>", redacted)
    redacted = _SENSITIVE_FLAG_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _SSHPASS_PASSWORD_RE.sub(r"\1<redacted>", redacted)
    redacted = _PACKAGE_CONFIG_SECRET_RE.sub(r"\1<redacted>", redacted)
    return redacted, redacted != command



def project_tool_args(args: dict, *, tool_name: str) -> tuple[dict | None, str]:
    if not isinstance(args, dict):
        raise ValueError("tool args must be an object")
    allowed = {
        "bash",
        "powershell",
        "read",
        "write",
        "replace",
        "manage",
        "git",
        "agent",
    }
    if tool_name not in allowed:
        return None, ""
    try:
        json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("tool args are not JSON serializable") from exc
    if tool_name in {"bash", "powershell"}:
        projected = dict(args)
        if "command" not in projected:
            projected = {}
        else:
            command, contains_sensitive_data = _redact_shell_command(str(projected["command"]))
            projected["command"] = command
            projected["shell_context"] = {
                "shell": tool_name,
                "working_directory": "workspace_root",
                "contains_sensitive_data": contains_sensitive_data,
            }
    elif tool_name in {"read", "write", "replace", "manage", "git"}:
        projected = {}
        for key in (
            "operation",
            "file_path",
            "path",
            "source",
            "dest",
            "command",
            "args",
            "subcommand",
        ):
            if key in args:
                projected[key] = args[key]
        for key in ("content", "new_string", "body"):
            if key in args and isinstance(args[key], str):
                projected[key] = {
                    "length": len(args[key]),
                    "sha256": hashlib.sha256(args[key].encode()).hexdigest(),
                }
    else:
        projected = {key: args[key] for key in ("agent", "mode", "target", "task", "description") if key in args}
        for key in ("task", "description"):
            if isinstance(projected.get(key), str):
                projected[key] = projected[key][:2000]
    redacted_projected = _redact(projected)
    if tool_name in {"bash", "powershell"} and redacted_projected != projected:
        redacted_projected["shell_context"]["contains_sensitive_data"] = True
    projected = redacted_projected
    encoded = json.dumps(projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("projected args exceed item limit")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return projected, digest

