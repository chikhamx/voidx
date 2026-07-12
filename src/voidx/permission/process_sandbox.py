"""Phase 6 process sandbox capability gate.

This module models whether shell tools have a verified process-level
filesystem sandbox backend available. The Phase 6 MVP intentionally fails
closed unless a test/verified backend is explicitly supplied by the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProcessSandboxBackend(str, Enum):
    NONE = "none"
    TEST = "test"
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


@dataclass(frozen=True)
class ProcessSandboxCapability:
    backend: ProcessSandboxBackend = ProcessSandboxBackend.NONE
    supported: bool = False
    bash: bool = False
    powershell: bool = False

    def usable_for(self, tool: str) -> bool:
        normalized = tool.lower()
        if not self.supported or self.backend == ProcessSandboxBackend.NONE:
            return False
        if self.backend == ProcessSandboxBackend.TEST:
            return normalized in {"bash", "powershell"}
        if normalized == "bash":
            return self.bash
        if normalized == "powershell":
            return self.powershell
        return False

    def denial_reason(self, tool: str) -> str:
        return f"process sandbox unavailable for {tool}"


def default_process_sandbox_capability() -> ProcessSandboxCapability:
    return ProcessSandboxCapability()
