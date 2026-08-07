"""Wildcard pattern matching — aligned with opencode Wildcard.match()."""

import re
import sys


def match(input_str: str, pattern: str) -> bool:
    """Match string against wildcard pattern.

    *  → .*
    ?  → .
    /  ↔ \\  normalized for cross-platform

    Examples:
      match("bash", "*") → True
      match("read", "edit") → False
      match(".env", "*.env") → True
      match("git push", "git *") → True
      match("src/foo.py", "src/**/*.py") → True  (** treated as *)
    """
    normalized = input_str.replace("\\", "/")

    escaped = (
        pattern.replace("\\", "/")
        .replace("**", "*")  # ** is same as * for our purposes
    )

    # Escape regex specials, then convert wildcards
    escaped = re.escape(escaped)
    escaped = escaped.replace(r"\*", ".*")
    escaped = escaped.replace(r"\?", ".")

    flags = re.IGNORECASE if sys.platform == "win32" else re.NOFLAG
    return bool(re.match("^" + escaped + "$", normalized, flags))
