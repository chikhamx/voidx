"""Slash command domain mixins."""

from voidx.presentation.slash.commands.mode import ModeCommandsMixin
from voidx.presentation.slash.commands.session import SessionCommandsMixin
from voidx.presentation.slash.commands.model import ModelCommandsMixin
from voidx.presentation.slash.commands.profile import ProfileCommandsMixin
from voidx.presentation.slash.commands.permission import PermissionCommandsMixin
from voidx.presentation.slash.commands.web import WebCommandsMixin
from voidx.presentation.slash.commands.ide import IdeCommandsMixin
from voidx.presentation.slash.commands.guide import GuideCommandsMixin
from voidx.presentation.slash.commands.lsp import LspCommandsMixin
from voidx.presentation.slash.commands.skills import SkillsCommandsMixin
from voidx.presentation.slash.commands.upgrade import UpgradeCommandsMixin
from voidx.presentation.slash.commands.mcp import McpCommandsMixin
from voidx.presentation.slash.commands.loop_cmd import LoopCmdCommandsMixin

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
