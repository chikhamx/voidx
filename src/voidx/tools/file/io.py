from __future__ import annotations

from pathlib import Path

from .safe_path import SafePathExecutor


def safe_read_text(path: Path, *, write_access: bool = False) -> tuple[str, str | None]:
    executor = SafePathExecutor()
    try:
        authorized = executor.authorize_existing(
            path,
            access="write" if write_access else "read",
        )
    except OSError as exc:
        return "", str(exc)
    result = executor.read_text(authorized, encoding="utf-8", errors="replace")
    if not result.ok:
        return "", result.error
    assert isinstance(result.value, str)
    return result.value, None


def safe_write_text(
    path: Path,
    content: str,
    *,
    require_exists: bool = False,
    expected_text: str | None = None,
) -> str | None:
    executor = SafePathExecutor()
    try:
        authorized = (
            executor.authorize_existing(path, access="write")
            if require_exists
            else executor.authorize_target(path, access="write")
        )
    except OSError as exc:
        return str(exc)
    result = executor.write_text(
        authorized,
        content,
        encoding="utf-8",
        expected_text=expected_text,
    )
    return None if result.ok else result.error
