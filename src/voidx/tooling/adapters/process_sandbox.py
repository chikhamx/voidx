"""Default process-sandbox capability detection."""

from __future__ import annotations

import platform
import shutil

from voidx.tooling.domain.process_sandbox import ProcessSandboxBackend, ProcessSandboxCapability


def detect_process_sandbox_capability(system: str | None = None) -> ProcessSandboxCapability:
    """Describe only verified, installed system sandbox backends."""
    normalized = (system or platform.system()).lower()
    if normalized == "darwin" and shutil.which("sandbox-exec"):
        return ProcessSandboxCapability(
            backend=ProcessSandboxBackend.MACOS,
            supported=True,
            bash=True,
        )
    if normalized == "linux" and shutil.which("bwrap"):
        return ProcessSandboxCapability(
            backend=ProcessSandboxBackend.LINUX,
            supported=True,
            bash=True,
        )
    return ProcessSandboxCapability()


def default_process_sandbox_capability() -> ProcessSandboxCapability:
    """Return the capability of the current host."""
    return detect_process_sandbox_capability()
