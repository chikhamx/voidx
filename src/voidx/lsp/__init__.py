"""Language Server Protocol integration."""

from __future__ import annotations

from voidx.lsp.config import LSP_CONFIG_FILE, load_lsp_servers
from voidx.lsp.schema import (
    LspDiagnostic,
    LspDoctorCheck,
    LspLocation,
    LspPosition,
    LspRange,
    LspRuntimeStatus,
    LspServerConfig,
    LspSymbol,
)


def __getattr__(name: str):
    if name == "LspManager":
        from voidx.lsp.manager import LspManager

        return LspManager
    raise AttributeError(name)


__all__ = [
    "LSP_CONFIG_FILE",
    "LspDiagnostic",
    "LspDoctorCheck",
    "LspLocation",
    "LspManager",
    "LspPosition",
    "LspRange",
    "LspRuntimeStatus",
    "LspServerConfig",
    "LspSymbol",
    "load_lsp_servers",
]
