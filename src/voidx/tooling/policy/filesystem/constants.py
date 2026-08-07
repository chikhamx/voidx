"""Filesystem policy classification constants."""

from __future__ import annotations

import re

REDIR_PATTERNS = [
    re.compile(r"\d?\s*>>?\s*(\S+)"),
    re.compile(r"\|\s*tee(?:\s+-a)?\s+(\S+)"),
    re.compile(r"\bof=(\S+)"),
]
FS_WRITE_COMMANDS = {
    "rm": 1,
    "cp": -1,
    "mv": -1,
    "ln": -1,
    "mkdir": 1,
    "touch": 1,
    "install": -1,
    "tee": 1,
    "chmod": 0,
    "chown": 0,
    "chgrp": 0,
}
FILE_PATTERN_TOOLS = {"read", "write", "replace", "lsp_format", "lsp"}
