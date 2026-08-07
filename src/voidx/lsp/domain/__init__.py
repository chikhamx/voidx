"""LSP domain models and errors."""

from voidx.lsp.domain.errors import (
    LspConnectionError,
    LspError,
    LspFormattingUnsupported,
    LspRequestError,
    LspServerUnavailable,
    LspTimeoutError,
)
from voidx.lsp.domain.schema import (
    LspDiagnostic,
    LspDoctorCheck,
    LspLocation,
    LspPosition,
    LspRange,
    LspRuntimeStatus,
    LspServerConfig,
    LspSymbol,
)

__all__ = [
    "LspConnectionError",
    "LspDiagnostic",
    "LspDoctorCheck",
    "LspError",
    "LspFormattingUnsupported",
    "LspLocation",
    "LspPosition",
    "LspRange",
    "LspRequestError",
    "LspRuntimeStatus",
    "LspServerConfig",
    "LspServerUnavailable",
    "LspSymbol",
    "LspTimeoutError",
]
