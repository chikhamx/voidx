"""Concrete LSP client adapter."""

from voidx.lsp.adapters.client.stdio import LspClient, encode_lsp_message
from voidx.lsp.ports.client import LSP_REQUEST_TIMEOUT_SECONDS


def create_lsp_client(config, cwd: str):
    return LspClient(config, cwd=cwd)


__all__ = ["LSP_REQUEST_TIMEOUT_SECONDS", "LspClient", "create_lsp_client", "encode_lsp_message"]
