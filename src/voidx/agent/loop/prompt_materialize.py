"""Materialize @path references in /loop prompts at start time.

Text documents are inlined as a one-time snapshot; executable scripts are kept
as references with guidance to run them at the start of every iteration.
"""

from __future__ import annotations

import os
from pathlib import Path

from voidx.agent.application.attachments import _ATTACHMENT_RE, _pasted_spans

_MAX_SNAPSHOT_BYTES = 200_000
_SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".py", ".pl", ".rb", ".js", ".mjs", ".ts"}


class PromptMaterializeError(ValueError):
    pass


def materialize_loop_prompt(prompt: str, workspace: str) -> str:
    refs = _reference_tokens(prompt)
    if not refs:
        return prompt
    root = Path(workspace).resolve()
    snapshots: list[tuple[str, str]] = []
    scripts: list[str] = []
    for raw in refs:
        if raw.startswith(":image:"):
            raise PromptMaterializeError(f"Image attachments are not supported in /loop prompts: {raw}")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise PromptMaterializeError(f"Referenced path not found: {raw}")
        if path.is_dir():
            raise PromptMaterializeError(f"Referenced path is a directory, not a file: {raw}")
        display = _display_path(root, path)
        if _is_script(path):
            scripts.append(display)
            continue
        snapshots.append((display, _read_snapshot(path, raw)))
    return _render(prompt, snapshots, scripts)


def _reference_tokens(text: str) -> list[str]:
    excluded = _pasted_spans(text)
    tokens: list[str] = []
    for match in _ATTACHMENT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in excluded):
            continue
        image_stem = match.group(3)
        if image_stem:
            raise PromptMaterializeError(
                f"Image attachments are not supported in /loop prompts: [image-{image_stem}]"
            )
        raw = match.group(1) or match.group(2)
        if raw:
            tokens.append(raw)
    return tokens


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_script(path: Path) -> bool:
    if path.suffix.lower() in _SCRIPT_SUFFIXES:
        return True
    if os.access(path, os.X_OK):
        return True
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _read_snapshot(path: Path, raw: str) -> str:
    blob = path.read_bytes()
    truncated = len(blob) > _MAX_SNAPSHOT_BYTES
    if truncated:
        blob = blob[:_MAX_SNAPSHOT_BYTES]
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not truncated:
            raise PromptMaterializeError(f"Referenced file is not a text file: {raw}") from exc
        text = blob.decode("utf-8", errors="replace").rstrip("\ufffd")
    if truncated:
        text += f"\n\n[... truncated at {_MAX_SNAPSHOT_BYTES} bytes ...]"
    return text


def _render(prompt: str, snapshots: list[tuple[str, str]], scripts: list[str]) -> str:
    parts = [prompt.rstrip()]
    if snapshots:
        sections = ["# Referenced documents (snapshot taken when this loop started)"]
        sections.extend(f"## {display}\n\n{content}" for display, content in snapshots)
        parts.append("\n\n".join(sections))
    if scripts:
        lines = "\n".join(f"- {display}" for display in scripts)
        parts.append(
            "# Task scripts\n"
            "At the start of every loop iteration, run each script below with the bash tool "
            "to fetch the current task content, then act on its output:\n" + lines
        )
    return "\n\n".join(parts)
