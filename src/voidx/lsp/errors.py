"""LSP error types."""

from __future__ import annotations


class LspError(Exception):
    """Base class for LSP failures."""


class LspConnectionError(LspError):
    """Raised when a language server process cannot be used."""


class LspRequestError(LspError):
    """Raised when a server returns a JSON-RPC error response."""


class LspServerUnavailable(LspError):
    """Raised when no enabled server can handle a file."""
