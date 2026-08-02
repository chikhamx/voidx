"""Semantic file discovery and content search tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from voidx.tools.file.state import persist_named_tool_result
from voidx.logging.tool_log import log_tool_event
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    _resolve_tool_path,
    _sandbox_paths_for_access,
    model_to_json_schema,
    SKIP_DIRS,
    SKIP_SUFFIXES,
)
from voidx.tools.file.state import record_read_range


CaseMode = Literal["auto", "sensitive", "insensitive"]
MatchMode = Literal["text", "word", "regex"]
OUTPUT_CHAR_BUDGET = 4_000


class FindInput(BaseModel):
    query: str | None = Field(default=None, description="Filename substring, without path separators.")
    path: str | None = Field(default=None, description="File or directory scope; defaults to workspace root.")
    extensions: list[str] | None = Field(default=None, description="Extensions such as ['py', 'pyi'].")
    case: CaseMode = "auto"
    max_results: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def _require_query_or_extensions(self):
        if not self.query and not self.extensions:
            raise ValueError("query or extensions is required")
        if self.query and any(sep in self.query for sep in ("/", "\\")):
            raise ValueError("query must not contain path separators")
        return self

    @field_validator("extensions", mode="before")
    @classmethod
    def _normalize_extensions(cls, value):
        if value is None:
            return value
        return [str(item).lstrip(".").lower() for item in value]


class SearchInput(BaseModel):
    query: str = Field(min_length=1, description="Text or regular expression to search for.")
    path: str | None = Field(default=None, description="File or directory scope; defaults to workspace root.")
    extensions: list[str] | None = None
    match: MatchMode = "text"
    case: CaseMode = "auto"
    context: int = Field(default=0, ge=0, le=10)
    max_results: int = Field(default=30, ge=1, le=500)

    @field_validator("extensions", mode="before")
    @classmethod
    def _normalize_extensions(cls, value):
        if value is None:
            return value
        return [str(item).lstrip(".").lower() for item in value]


class _FileEntry(BaseModel):
    path: Path
    relative: str


def _load_gitignore(base: Path):
    try:
        import pathspec
    except ImportError:
        return None
    target = base / ".gitignore"
    if not target.is_file():
        return None
    try:
        return pathspec.PathSpec.from_lines("gitignore", target.read_text(encoding="utf-8").splitlines())
    except Exception:
        log_tool_event("tool_gitignore_parse_failed", tool_name="search", message="Failed to parse .gitignore")
        return None


def _relative(base: Path, path: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


def _hidden_content(relative: str) -> bool:
    parts = Path(relative).parts
    return any(part.startswith(".") for part in parts[:-1])


def _ignored(spec, relative: str) -> bool:
    if spec is None:
        return False
    return bool(spec.match_file(relative) or spec.match_file(relative + "/"))


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    try:
        return b"\x00" in path.read_bytes()[:8192]
    except OSError:
        return False


def _visible_files(base: Path, scope: Path, *, skip_binary: bool):
    spec = _load_gitignore(base)
    candidates: list[Path] = []
    if scope.is_file():
        candidates = [scope]
    elif scope.is_dir():
        for root, dirs, files in os.walk(scope, topdown=True, followlinks=False):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
            candidates.extend(root_path / name for name in files)
    for path in sorted(candidates, key=lambda item: _relative(base, item)):
        if path.is_symlink():
            continue
        try:
            relative = _relative(base, path)
        except ValueError:
            continue
        if _hidden_content(relative):
            continue
        if _ignored(spec, relative) or (skip_binary and _is_binary(path)):
            continue
        yield _FileEntry(path=path, relative=relative)


def _resolve_scope(ctx: ToolContext, value: str | None):
    base = Path(ctx.workspace)
    scope = _resolve_tool_path(ctx.workspace, value, _sandbox_paths_for_access(ctx, write=False)) if value else base
    return base, scope


def _case_insensitive(value: str, mode: CaseMode) -> bool:
    return mode == "insensitive" or (mode == "auto" and not any(char.isupper() for char in value))


def _json_output(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _overflow_name(tool_name: str, full_output: str) -> str:
    digest = hashlib.sha256(full_output.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}-overflow-{digest}"


def _fit_json_output(
    build_payload,
    size: int,
    *,
    already_truncated: bool,
    ctx: ToolContext,
    tool_name: str,
    max_chars: int = OUTPUT_CHAR_BUDGET,
) -> tuple[str, int, bool]:
    full_payload = build_payload(size, already_truncated)
    full_output = _json_output(full_payload)
    if len(full_output) <= max_chars:
        return full_output, size, already_truncated

    overflow_path = None
    try:
        overflow_path = persist_named_tool_result(
            full_output,
            _overflow_name(tool_name, full_output),
            session_id=ctx.session_id,
            workspace=ctx.workspace,
        )
    except OSError:
        overflow_path = None

    for count in range(size - 1, -1, -1):
        payload = build_payload(count, True)
        if overflow_path:
            payload["overflow_path"] = overflow_path
        text = _json_output(payload)
        if len(text) <= max_chars or count == 0:
            return text, count, True
    payload = build_payload(0, True)
    if overflow_path:
        payload["overflow_path"] = overflow_path
    return _json_output(payload), 0, True


class FindTool(BaseTool):
    id = "find"
    description = "Find files by filename substring and extension filters with stable structured results."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FindInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = FindInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        base, scope = _resolve_scope(ctx, inp.path)
        if scope is None:
            return ToolResult(output=f"Path traversal blocked: {inp.path}", metadata={"error": True})
        if not scope.exists():
            return ToolResult(output=f"Path not found: {inp.path}", metadata={"error": True})
        query = inp.query or ""
        needle = query if not _case_insensitive(query, inp.case) else query.lower()
        extensions = set(inp.extensions or [])
        matches = []
        for entry in _visible_files(base, scope, skip_binary=False):
            candidate = entry.path.name if not _case_insensitive(query, inp.case) else entry.path.name.lower()
            if query and needle not in candidate:
                continue
            if extensions and entry.path.suffix.lstrip(".").lower() not in extensions:
                continue
            rank = 0 if query and candidate == needle else 1 if query and candidate.startswith(needle) else 2
            matches.append((rank, entry.relative, {"path": entry.relative, "name": entry.path.name}))
        matches.sort(key=lambda item: (item[0], item[1]))
        limited = [item[2] for item in matches[:inp.max_results]]
        count_truncated = len(matches) > inp.max_results

        def build_payload(count: int, truncated: bool) -> dict:
            return {
                "query": inp.query,
                "extensions": inp.extensions,
                "files": limited[:count],
                "truncated": truncated,
            }

        output, returned, truncated = _fit_json_output(
            build_payload,
            len(limited),
            already_truncated=count_truncated,
            ctx=ctx,
            tool_name="find",
        )
        return ToolResult(
            output=output,
            summary=f"{returned} files",
            metadata={"count": returned, "truncated": truncated},
        )


class SearchTool(BaseTool):
    id = "search"
    description = "Search text with literal, word, or regex matching and structured results."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(SearchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = SearchInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        base, scope = _resolve_scope(ctx, inp.path)
        if scope is None:
            return ToolResult(output=f"Path traversal blocked: {inp.path}", metadata={"error": True})
        if not scope.exists():
            return ToolResult(output=f"Path not found: {inp.path}", metadata={"error": True})
        flags = re.IGNORECASE if _case_insensitive(inp.query, inp.case) else 0
        expression = re.escape(inp.query) if inp.match in ("text", "word") else inp.query
        if inp.match == "word":
            expression = rf"(?<!\w){expression}(?!\w)"
        try:
            regex = re.compile(expression, flags)
        except re.error as exc:
            return ToolResult(output=f"Invalid regex: {exc}", metadata={"error": True})
        extensions = set(inp.extensions or [])
        grouped: dict[str, list[dict]] = {}
        count = 0
        truncated = False
        for entry in _visible_files(base, scope, skip_binary=True):
            if extensions and entry.path.suffix.lstrip(".").lower() not in extensions:
                continue
            try:
                lines = entry.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                log_tool_event("search_read_failed", tool_name="search", message=str(exc))
                continue
            hits = []
            for line_no, line in enumerate(lines, 1):
                match = regex.search(line)
                if not match:
                    continue
                if count >= inp.max_results:
                    truncated = True
                    break
                count += 1
                hits.append({"line": line_no, "column": match.start() + 1, "text": line[:200], "before": [], "after": []})
            if hits:
                if inp.context:
                    for hit in hits:
                        start = max(1, hit["line"] - inp.context)
                        end = min(len(lines), hit["line"] + inp.context)
                        hit["before"] = [{"line": n, "text": lines[n - 1][:200]} for n in range(start, hit["line"])]
                        hit["after"] = [{"line": n, "text": lines[n - 1][:200]} for n in range(hit["line"] + 1, end + 1)]
                grouped[entry.relative] = hits
                record_read_range(ctx, entry.path, min(hit["line"] for hit in hits), max(hit["line"] for hit in hits))
            if truncated:
                break
        flat_hits = [(path, hit) for path, hits in grouped.items() for hit in hits]

        def build_payload(hit_count: int, is_truncated: bool) -> dict:
            selected: dict[str, list[dict]] = {}
            for path, hit in flat_hits[:hit_count]:
                selected.setdefault(path, []).append(hit)
            return {
                "query": inp.query,
                "match": inp.match,
                "case": inp.case,
                "matches": [{"path": path, "hits": hits} for path, hits in selected.items()],
                "truncated": is_truncated,
            }

        output, returned, truncated = _fit_json_output(
            build_payload,
            len(flat_hits),
            already_truncated=truncated,
            ctx=ctx,
            tool_name="search",
        )
        selected_hits = flat_hits[:returned]
        details = [{"path": path, **hit} for path, hit in selected_hits]
        display = "\n".join(f"{path}:{hit['line']}:{hit['text']}" for path, hit in selected_hits)
        return ToolResult(
            output=output,
            display=display,
            summary=f"{returned} matches",
            metadata={"query": inp.query, "matches": returned, "match_details": details, "truncated": truncated},
        )
