"""LSP config shim — returns the LSP configuration file path."""

from pathlib import Path


def lsp_config_path(workspace: str | Path = ".") -> Path:
    return Path(workspace) / ".voidx" / "lsp.json"
