"""LSP server configuration loading."""

from __future__ import annotations

import json
from pathlib import Path

from voidx.lsp.schema import LspServerConfig
from voidx.paths import LSP_CONFIG_FILE

DEFAULT_SERVERS: dict[str, LspServerConfig] = {
    "python": LspServerConfig(
        language="python",
        command="pyright-langserver",
        args=["--stdio"],
        extensions=[".py"],
    ),
    "typescript": LspServerConfig(
        language="typescript",
        command="typescript-language-server",
        args=["--stdio"],
        extensions=[".ts", ".tsx"],
    ),
    "javascript": LspServerConfig(
        language="javascript",
        command="typescript-language-server",
        args=["--stdio"],
        extensions=[".js", ".jsx"],
    ),
    "go": LspServerConfig(
        language="go",
        command="gopls",
        extensions=[".go"],
    ),
    "rust": LspServerConfig(
        language="rust",
        command="rust-analyzer",
        extensions=[".rs"],
    ),
    "c": LspServerConfig(
        language="c",
        command="clangd",
        extensions=[".c", ".h"],
    ),
    "cpp": LspServerConfig(
        language="cpp",
        command="clangd",
        extensions=[".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"],
    ),
}

INSTALL_HINTS: dict[str, str] = {
    "python": "Install with: npm install -g pyright",
    "typescript": "Install with: npm install -g typescript typescript-language-server",
    "javascript": "Install with: npm install -g typescript typescript-language-server",
    "go": "Install with: go install golang.org/x/tools/gopls@latest",
    "rust": "Install with: rustup component add rust-analyzer",
    "c": "Install with: brew install llvm  (or xcode-select --install for Xcode CLT)",
    "cpp": "Install with: brew install llvm  (or xcode-select --install for Xcode CLT)",
}


def install_hint_for(language: str) -> str:
    return INSTALL_HINTS.get(language, "")


def lsp_config_path(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / LSP_CONFIG_FILE


def load_lsp_servers(workspace: str | Path) -> dict[str, LspServerConfig]:
    """Load LSP server configs: defaults < user config < auto-detection.

    Resolution order:
      1. Default built-in servers
      2. User overrides from .voidx/lsp.json
      3. Mark default servers whose command is already on PATH
      4. Auto-detection for servers whose command isn't already resolvable
    """
    servers = {name: config.model_copy(deep=True) for name, config in DEFAULT_SERVERS.items()}
    config_path = lsp_config_path(workspace)
    if config_path.exists():
        _apply_user_config(servers, config_path)
    _mark_path_servers(servers)
    _apply_auto_detection(servers)
    return servers


def _apply_user_config(servers: dict[str, LspServerConfig], config_path: Path) -> None:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    raw_servers = data.get("servers", {}) if isinstance(data, dict) else {}
    if not isinstance(raw_servers, dict):
        return
    for language, fields in raw_servers.items():
        if not isinstance(fields, dict):
            continue
        base = servers.get(language, LspServerConfig(language=language, command=""))
        merged = base.model_dump()
        merged.update(fields)
        merged["language"] = language
        try:
            servers[language] = LspServerConfig(**merged)
        except ValueError:
            continue


def _apply_auto_detection(servers: dict[str, LspServerConfig]) -> None:
    """Run auto-detection for servers whose command cannot be resolved.

    Only applies to servers that:
      - Are enabled
      - Don't already have a resolved_command
      - Still use the DEFAULT_SERVERS command (user overrides skip detection)
      - Can be found via auto-detection
    """
    from voidx.lsp.detector import detect_servers, resolve_command

    # Collect languages that need detection — only those still on default command
    needs_detection = [
        lang for lang, cfg in servers.items()
        if (
            cfg.enabled
            and not cfg.resolved_command
            and cfg.command == DEFAULT_SERVERS.get(lang, LspServerConfig(language=lang, command="")).command
        )
    ]
    if not needs_detection:
        return

    detected = detect_servers()
    for language in needs_detection:
        if language not in detected:
            continue
        d = detected[language]
        # Only apply if we found an actual executable
        if d.resolved_command:
            servers[language] = LspServerConfig(
                language=language,
                command=d.resolved_command,
                args=d.args,
                extensions=servers[language].extensions,
                enabled=True,
                resolved_command=d.resolved_command,
                detected_source=d.detected_source,
            )


def _mark_path_servers(servers: dict[str, LspServerConfig]) -> None:
    """Mark servers whose default command is already on PATH."""
    from voidx.lsp.detector import resolve_command

    for language, cfg in servers.items():
        if not cfg.enabled or cfg.resolved_command:
            continue
        resolved = resolve_command(cfg.command)
        if resolved:
            cfg.resolved_command = resolved
            cfg.detected_source = "PATH"


def language_for_path(path: str | Path, servers: dict[str, LspServerConfig]) -> str | None:
    suffix = Path(path).suffix.lower()
    for language, config in servers.items():
        if not config.enabled:
            continue
        if suffix in {ext.lower() for ext in config.extensions}:
            return language
    return None
