"""Resolve /loop prompt sources at trigger time."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any, Literal

from voidx.agent.attachments import MAX_TEXT_ATTACHMENT_BYTES, PathReference, _attachment_tokens

PromptSourceKind = Literal["text", "file", "script", "mixed"]
_SCRIPT_EXTENSIONS = {".sh", ".py"}


@dataclass(frozen=True)
class PromptSource:
    raw: str
    kind: PromptSourceKind
    references: list[PathReference]

    @classmethod
    def from_raw(cls, raw: str) -> "PromptSource":
        references = [
            PathReference(raw_path=raw_path, display_path=raw_path, token_span=(start, end))
            for start, end, raw_path in _attachment_tokens(raw)
            if not raw_path.startswith(":image:")
        ]
        if not references:
            kind: PromptSourceKind = "text"
        elif len(references) == 1 and raw.strip() == raw[references[0].token_span[0]:references[0].token_span[1]]:
            kind = "script" if _looks_like_script_reference(references[0].raw_path) else "file"
        else:
            kind = "mixed"
        return cls(raw=raw, kind=kind, references=references)

    async def resolve(
        self,
        workspace: str,
        *,
        bash_tool: Any | None = None,
        ctx: Any | None = None,
    ) -> str:
        if not self.references:
            return self.raw
        if len(self.references) > 1:
            return _loop_error("multiple @ references are not supported yet")

        ref = self.references[0]
        resolved, error = _resolve_workspace_file(workspace, ref.raw_path)
        if error is not None:
            return _loop_error(error)
        assert resolved is not None

        prefix = self.raw[:ref.token_span[0]].strip()
        suffix = self.raw[ref.token_span[1]:].strip()
        content = await self._resolve_reference(resolved, bash_tool=bash_tool, ctx=ctx)
        if content.startswith("[loop] prompt source error:"):
            return content
        parts = [part for part in [prefix, content, suffix] if part]
        result = "\n".join(parts)
        if not result.strip():
            return _loop_error(f"prompt source produced no output: {ref.display_path}")
        return result

    async def _resolve_reference(
        self,
        path: Path,
        *,
        bash_tool: Any | None,
        ctx: Any | None,
    ) -> str:
        if _is_script_file(path):
            if bash_tool is None or ctx is None:
                return _loop_error(f"script prompt requires bash tool context: {path.name}")
            command = _script_command(path)
            result = await bash_tool.execute({"command": command, "timeout": 30}, ctx)
            if result.metadata.get("error"):
                return _loop_error(result.output)
            if not result.output.strip():
                return _loop_error(f"prompt source produced no output: {path.name}")
            return result.output
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _loop_error(str(exc))
        if len(content) > MAX_TEXT_ATTACHMENT_BYTES:
            content = content[:MAX_TEXT_ATTACHMENT_BYTES]
        if not content.strip():
            return _loop_error(f"prompt source produced no output: {path.name}")
        return content


def _resolve_workspace_file(workspace: str, raw_path: str) -> tuple[Path | None, str | None]:
    workspace_path = Path(workspace).resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_path / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, str(exc)
    if workspace_path != resolved and workspace_path not in resolved.parents:
        return None, f"path is outside workspace: {raw_path}"
    if not resolved.exists():
        return None, f"file not found: {raw_path}"
    if not resolved.is_file():
        return None, f"prompt source is not a regular file: {raw_path}"
    return resolved, None


def _looks_like_script_reference(raw_path: str) -> bool:
    return Path(raw_path).suffix.lower() in _SCRIPT_EXTENSIONS


def _is_script_file(path: Path) -> bool:
    if path.suffix.lower() in _SCRIPT_EXTENSIONS:
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def _script_command(path: Path) -> str:
    quoted = shlex.quote(str(path))
    if path.suffix.lower() == ".sh" and not path.stat().st_mode & 0o111:
        return f"bash {quoted}"
    if path.suffix.lower() == ".py" and not path.stat().st_mode & 0o111:
        return f"python {quoted}"
    return quoted


def _loop_error(message: str) -> str:
    return f"[loop] prompt source error: {message}"
