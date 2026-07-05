"""ANSI line-prefix marker — single source of truth.

The marker separates rich-markup text from ANSI-styled text within a
single render line. Shared by ``tree.py`` (renderer) and
``dock/formatting.py`` (capture) to avoid duplicating the literal.
"""

from __future__ import annotations

ANSI_LINE_PREFIX = "\x00voidx-ansi\x00"
