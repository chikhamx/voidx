"""Shared inline reference token patterns."""

from __future__ import annotations

import re

EXPLICIT_REF_RE = re.compile(r"(?<![\w.-])\$([A-Za-z0-9_.-]+)")

__all__ = ["EXPLICIT_REF_RE"]
