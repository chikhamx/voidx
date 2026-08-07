"""PowerShell tool package — execute commands + route hint detection (Windows)."""

from voidx.tooling.builtin.shell.powershell.tool import PowerShellInput, PowerShellTool
from voidx.tooling.builtin.shell.powershell.router import try_hint
from voidx.tooling.builtin.shell.common import RouteHint

__all__ = ["PowerShellInput", "PowerShellTool", "RouteHint", "try_hint"]
