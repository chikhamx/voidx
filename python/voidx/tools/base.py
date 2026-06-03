"""Shim — path safety and base types for tools."""

from pathlib import Path


def resolve_safe(workspace: str, file_path: str, extra_paths: list[str] | None = None) -> Path | None:
    """Resolve file path and verify it stays inside workspace."""
    ws = Path(workspace).resolve()
    resolved = (ws / file_path).resolve()

    allowed = [ws]
    if extra_paths:
        for ep in extra_paths:
            allowed.append(Path(ep).expanduser().resolve())

    for base in allowed:
        try:
            resolved.relative_to(base)
            # Ensure no '..' traversal
            parts = resolved.relative_to(base).parts
            if ".." not in parts:
                return resolved
        except ValueError:
            continue
    return None
