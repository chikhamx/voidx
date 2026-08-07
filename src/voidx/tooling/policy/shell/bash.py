"""Bash hard-block decision policy."""

from voidx.tooling.domain.risk import RiskLevel
from voidx.tooling.policy.shell.policy import classify_shell_risk


def blocked_command_reason(command: str) -> str | None:
    risk = classify_shell_risk(command, shell="bash")
    return risk.reason if risk.level == RiskLevel.BLOCKED else None
