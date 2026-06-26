"""Backward-compatible re-exports for the renamed SkillsTool.

LoadSkillsTool has been superseded by SkillsTool (src/voidx/tools/skills.py).
This module preserves import compatibility for existing test fixtures.
"""

from __future__ import annotations

from voidx.tools.skills import SkillsInput as LoadSkillsInput
from voidx.tools.skills import SkillsTool as LoadSkillsTool

__all__ = ["LoadSkillsTool", "LoadSkillsInput"]
