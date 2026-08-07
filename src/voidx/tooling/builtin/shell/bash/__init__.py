"""Bash tool package — execute shell commands + route hint detection."""

from voidx.tooling.builtin.shell.bash.tool import BashInput, BashTool
from voidx.tooling.builtin.shell.bash.router import RouteHint, try_hint

__all__ = ["BashInput", "BashTool", "RouteHint", "try_hint"]
