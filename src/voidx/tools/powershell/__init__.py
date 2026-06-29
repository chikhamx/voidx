"""PowerShell tool package — execute commands + route hint detection (Windows)."""

from voidx.tools.powershell.tool import PowerShellInput, PowerShellTool
from voidx.tools.powershell.router import try_hint
from voidx.tools.shell.common import RouteHint

__all__ = ["PowerShellInput", "PowerShellTool", "RouteHint", "try_hint"]
