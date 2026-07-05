"""Default model/provider constants — single source of truth.

Centralizes the default model name and provider so that upgrades only
require changing one place. The SQLite schema default in ``store.py``
keeps a string literal for the DDL ``DEFAULT`` clause but should mirror
``DEFAULT_MODEL``.
"""

from __future__ import annotations

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"
