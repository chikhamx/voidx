"""Bash tool package — execute shell commands + route hint detection."""

from voidx.tools.bash.tool import BashInput, BashTool
from voidx.tools.bash.router import RouteHint, try_hint

__all__ = ["BashInput", "BashTool", "RouteHint", "try_hint"]
