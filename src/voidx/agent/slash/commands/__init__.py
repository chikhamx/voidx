"""Slash command domain mixins."""

from voidx.agent.slash.commands.mode import ModeCommandsMixin
from voidx.agent.slash.commands.session import SessionCommandsMixin
from voidx.agent.slash.commands.model import ModelCommandsMixin
from voidx.agent.slash.commands.profile import ProfileCommandsMixin
from voidx.agent.slash.commands.permission import PermissionCommandsMixin
from voidx.agent.slash.commands.web import WebCommandsMixin
from voidx.agent.slash.commands.ide import IdeCommandsMixin
from voidx.agent.slash.commands.guide import GuideCommandsMixin
from voidx.agent.slash.commands.lsp import LspCommandsMixin
from voidx.agent.slash.commands.skills import SkillsCommandsMixin
from voidx.agent.slash.commands.upgrade import UpgradeCommandsMixin
from voidx.agent.slash.commands.mcp import McpCommandsMixin
from voidx.agent.slash.commands.loop_cmd import LoopCmdCommandsMixin

__all__ = [
    "ModeCommandsMixin",
    "SessionCommandsMixin",
    "ModelCommandsMixin",
    "ProfileCommandsMixin",
    "PermissionCommandsMixin",
    "WebCommandsMixin",
    "IdeCommandsMixin",
    "GuideCommandsMixin",
    "LspCommandsMixin",
    "SkillsCommandsMixin",
    "UpgradeCommandsMixin",
    "McpCommandsMixin",
    "LoopCmdCommandsMixin",
]
