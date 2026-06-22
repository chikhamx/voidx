"""Search tools — glob pattern matching, grep content search. Fast, deterministic."""

from __future__ import annotations

import logging
import fnmatch
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from voidx.logging.tool_log import log_tool_event
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult, resolve_safe, SKIP_DIRS, SKIP_SUFFIXES

_logger = logging.getLogger(__name__)


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob pattern to match files, e.g. '**/*.py' or 'src/**/*.ts'")
    ignore_case: bool = Field(default=False, description="Case-insensitive glob matching when true")
    max_depth: int | None = Field(default=None, ge=0, description="Maximum path depth from workspace root when set")


class GlobTool(BaseTool):
    id = "glob"
    description = (
        "Find files by glob pattern (e.g. '**/*.py'). Returns sorted relative paths. "
        "Skips .git, node_modules, .venv, __pycache__, and other build/dot directories."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GlobInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = GlobInput.model_validate(args)
        base = Path(ctx.workspace)

        def _visible_path(p: Path) -> bool:
            parts = p.relative_to(base).parts
            for i, part in enumerate(parts):
                if part in SKIP_DIRS:
                    return False
                # Allow dot-files at root level (e.g. .env, .gitignore)
                # but skip content inside hidden directories
                if part.startswith(".") and i < len(parts) - 1:
                    return False
            return True

        def _within_depth(p: Path) -> bool:
            if inp.max_depth is None:
                return True
            return len(p.relative_to(base).parts) <= inp.max_depth

        def _relative(p: Path) -> str:
            return str(p.relative_to(base)).replace("\\", "/")

        def _glob_pattern_variants(pattern: str) -> set[str]:
            variants = {pattern}
            pending = [pattern]
            while pending:
                current = pending.pop()
                idx = current.find("**/")
                while idx != -1:
                    variant = current[:idx] + current[idx + 3:]
                    if variant not in variants:
                        variants.add(variant)
                        pending.append(variant)
                    idx = current.find("**/", idx + 1)
            return variants

        if inp.ignore_case:
            patterns = _glob_pattern_variants(inp.pattern.lower())
            candidates = (
                p for p in base.rglob("*")
                if any(fnmatch.fnmatchcase(_relative(p).lower(), pattern) for pattern in patterns)
            )
        else:
            candidates = base.glob(inp.pattern)

        matches = sorted(
            _relative(p)
            for p in candidates
            if _visible_path(p) and _within_depth(p)
        )

        if not matches:
            payload = {"pattern": inp.pattern, "matches": 0, "truncated": False, "files": []}
            return ToolResult(
                output=json.dumps(payload, ensure_ascii=False),
                display=f"No files matched pattern: {inp.pattern}",
                metadata={"pattern": inp.pattern, "matches": 0},
            )

        total = len(matches)
        shown = matches[:200]
        truncated = total > 200
        payload = {"pattern": inp.pattern, "matches": total, "truncated": truncated, "files": shown}
        return ToolResult(
            title=f"Glob: {inp.pattern} → {total} files",
            output=json.dumps(payload, ensure_ascii=False),
            display="\n".join(shown),
            summary=f"{total} files matched",
            metadata={"pattern": inp.pattern, "matches": total, "truncated": truncated},
        )


class GrepInput(BaseModel):
    pattern: str = Field(description="Regular expression to search for")
    path: str | None = Field(default=None, description="File or directory to search. Defaults to workspace root.")
    include: str | None = Field(default=None, description="Glob pattern to filter files, e.g. '*.py'")
    ignore_case: bool = Field(default=False, description="Case-insensitive search when true")
    whole_word: bool = Field(default=False, description="Match whole words only (adds \\b boundaries). Do not add \\b to pattern when using this.")
    context_lines: int = Field(default=0, ge=0, description="Number of context lines before and after each match (0 = none)")
    exclude: list[str] | None = Field(default=None, description="Glob patterns to exclude files, e.g. ['*.min.js', '*.map']")
    max_matches: int = Field(default=100, ge=1, description="Maximum number of matches to return")
    max_scanned: int = Field(default=5000, ge=1, description="Maximum number of files to scan")

    @field_validator("exclude", mode="before")
    @classmethod
    def _normalize_exclude(cls, v):
        if isinstance(v, str):
            return [v] if v else None
        return v


class GrepTool(BaseTool):
    id = "grep"
    description = (
        "Search file contents with regex. Returns file:line:content matches. "
        "Skips .git, node_modules, binary files, and build/dot directories. "
        "Supports case-insensitive search (ignore_case), whole-word matching (whole_word), "
        "context lines around matches (context_lines), and file exclusion (exclude)."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GrepInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = GrepInput.model_validate(args)
        base = Path(ctx.workspace)
        search_dir = resolve_safe(ctx.workspace, inp.path, ctx.sandbox_extra_paths) if inp.path else base
        if search_dir is None:
            return ToolResult(output=f"Path traversal blocked: {inp.path}")

        pattern = rf"\b{inp.pattern}\b" if inp.whole_word else inp.pattern
        flags = re.IGNORECASE if inp.ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(output=f"Invalid regex: {e}")

        gitignore_spec = _load_gitignore(base)

        results: list[str] = []
        match_details: list[dict] = []
        count = 0
        scanned = 0

        def should_skip_dir(p: Path) -> bool:
            if p.name in SKIP_DIRS:
                return True
            if gitignore_spec is not None:
                try:
                    rel = str(p.relative_to(base)).replace("\\", "/") + "/"
                    if gitignore_spec.match_file(rel):
                        return True
                except ValueError:
                    pass
            return False

        def is_binary(f: Path) -> bool:
            if f.suffix in SKIP_SUFFIXES:
                return True
            try:
                chunk = f.read_bytes()[:8192]
                return b"\x00" in chunk
            except Exception:
                return False

        def is_gitignored(f: Path) -> bool:
            if gitignore_spec is None:
                return False
            try:
                rel = str(f.relative_to(base)).replace("\\", "/")
                return gitignore_spec.match_file(rel)
            except ValueError:
                return False

        def iter_files(dir_path: Path):
            nonlocal scanned
            try:
                for entry in dir_path.iterdir():
                    if should_skip_dir(entry):
                        continue
                    if entry.is_dir():
                        yield from iter_files(entry)
                    elif entry.is_file():
                        if is_binary(entry):
                            continue
                        if is_gitignored(entry):
                            continue
                        if inp.include and not entry.match(inp.include):
                            continue
                        if inp.exclude and any(entry.match(g) for g in inp.exclude):
                            continue
                        scanned += 1
                        yield entry
            except PermissionError:
                pass

        if search_dir.is_file():
            files = [search_dir]
        elif search_dir.is_dir():
            files = iter_files(search_dir)
        else:
            return ToolResult(output=f"Path not found: {inp.path}")

        _MAX_OUTPUT_LINES = 500
        truncated = False

        for f in files:
            if scanned > inp.max_scanned:
                truncated = True
                results.append(f"... (stopped after scanning {scanned} files)")
                break

            try:
                lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
                hits: list[tuple[str, int, str, int]] = []
                for i, line in enumerate(lines, 1):
                    m = regex.search(line)
                    if m:
                        rel = str(f.relative_to(base)).replace("\\", "/")
                        hits.append((rel, i, line.strip()[:200], m.start()))
                        count += 1
                        if count >= inp.max_matches:
                            break
                if inp.context_lines > 0 and hits:
                    shown_lines: set[tuple[str, int]] = set()
                    for rel, line_no, content, col in hits:
                        results.append(f"{rel}:{line_no}:{content}")
                        match_details.append({"file": rel, "line": line_no, "column": col + 1, "content": content})
                        shown_lines.add((rel, line_no))
                        start = max(1, line_no - inp.context_lines)
                        end = min(len(lines), line_no + inp.context_lines)
                        for ctx_no in range(start, end + 1):
                            if ctx_no != line_no and (rel, ctx_no) not in shown_lines:
                                results.append(f"{rel}-{ctx_no}-{lines[ctx_no - 1].strip()[:200]}")
                                shown_lines.add((rel, ctx_no))
                        if len(results) >= _MAX_OUTPUT_LINES:
                            truncated = True
                            break
                else:
                    for rel, line_no, content, col in hits:
                        results.append(f"{rel}:{line_no}:{content}")
                        match_details.append({"file": rel, "line": line_no, "column": col + 1, "content": content})
                if count >= inp.max_matches:
                    truncated = True
                    break
                if len(results) >= _MAX_OUTPUT_LINES:
                    truncated = True
                    break
            except Exception as exc:
                _logger.debug("Failed to read file during grep: %s: %s", f, exc, exc_info=True)
                log_tool_event("grep_read_failed", tool_name="search", message=f"Failed to read file during grep: {f}: {exc}")
                continue

        if not results:
            payload = {"pattern": inp.pattern, "matches": 0, "truncated": False, "results": []}
            return ToolResult(
                output=json.dumps(payload, ensure_ascii=False),
                display=f"No matches found for: {inp.pattern}",
                metadata={"pattern": inp.pattern, "matches": 0, "match_details": [], "truncated": False},
            )

        payload = {"pattern": inp.pattern, "matches": count, "truncated": truncated, "results": match_details}
        return ToolResult(
            title=f"Grep: {inp.pattern} → {count} matches",
            output=json.dumps(payload, ensure_ascii=False),
            display="\n".join(results),
            summary=f"{count} matches",
            metadata={"pattern": inp.pattern, "matches": count, "match_details": match_details, "truncated": truncated},
        )


def _load_gitignore(base: Path):
    try:
        import pathspec
    except ImportError:
        return None
    gitignore_path = base / ".gitignore"
    if not gitignore_path.is_file():
        return None
    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except Exception:
        _logger.debug("Failed to parse .gitignore", exc_info=True)
        return None
