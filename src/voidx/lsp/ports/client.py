"""Port for an LSP client and its construction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from voidx.lsp.domain import LspServerConfig

LSP_REQUEST_TIMEOUT_SECONDS = 30.0


class LspClient(Protocol):
    @property
    def pid(self) -> int | None: ...

    @property
    def connected(self) -> bool: ...

    @property
    def error_message(self) -> str: ...

    @property
    def capabilities(self) -> dict[str, Any]: ...

    async def start(self, *, root_uri: str, timeout: float = LSP_REQUEST_TIMEOUT_SECONDS) -> None: ...

    async def stop(self) -> None: ...

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float = LSP_REQUEST_TIMEOUT_SECONDS,
    ) -> Any: ...

    async def notify(self, method: str, params: Any = None) -> None: ...

    def diagnostics_for(self, uri: str) -> list: ...

    def all_diagnostics(self) -> list: ...


LspClientFactory = Callable[[LspServerConfig, str], LspClient]

__all__ = ["LSP_REQUEST_TIMEOUT_SECONDS", "LspClient", "LspClientFactory"]
