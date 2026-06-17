"""Search tools — glob pattern matching, grep content search. Fast, deterministic."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from voidx.logging.tool_log import log_tool_event
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult, resolve_safe, SKIP_DIRS, SKIP_SUFFIXES

_logger = logging.getLogger(__name__)


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob pattern to match files, e.g. '**/*.py' or 'src/**/*.ts'")


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

        matches = sorted(
            str(p.relative_to(base)).replace("\\", "/")
            for p in base.glob(inp.pattern)
            if _visible_path(p)
        )

        if not matches:
            return ToolResult(
                output=f"No files matched pattern: {inp.pattern}",
                metadata={"pattern": inp.pattern, "matches": 0},
            )

        total = len(matches)
        shown = matches[:200]
        return ToolResult(
            title=f"Glob: {inp.pattern} → {total} files",
            output="\n".join(shown),
            summary=f"{total} files matched",
            metadata={"pattern": inp.pattern, "matches": total, "truncated": total > 200},
        )


class GrepInput(BaseModel):
    pattern: str = Field(description="Regular expression to search for")
    path: str | None = Field(default=None, description="File or directory to search. Defaults to workspace root.")
    include: str | None = Field(default=None, description="Glob pattern to filter files, e.g. '*.py'")


class GrepTool(BaseTool):
    id = "grep"
    description = (
        "Search file contents with regex. Returns file:line:content matches. "
        "Skips .git, node_modules, binary files, and build/dot directories."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GrepInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = GrepInput.model_validate(args)
        base = Path(ctx.workspace)
        search_dir = resolve_safe(ctx.workspace, inp.path, ctx.sandbox_extra_paths) if inp.path else base
        if search_dir is None:
            return ToolResult(output=f"Path traversal blocked: {inp.path}")

        try:
            regex = re.compile(inp.pattern)
        except re.error as e:
            return ToolResult(output=f"Invalid regex: {e}")

        results: list[str] = []
        count = 0
        scanned = 0

        def should_skip_dir(p: Path) -> bool:
            return p.name in SKIP_DIRS or p.name.startswith(".")

        def iter_files(dir_path: Path):
            nonlocal scanned
            try:
                for entry in dir_path.iterdir():
                    if should_skip_dir(entry):
                        continue
                    if entry.is_dir():
                        yield from iter_files(entry)
                    elif entry.is_file():
                        if entry.suffix in SKIP_SUFFIXES:
                            continue
                        if inp.include and not entry.match(inp.include):
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

        for f in files:
            if scanned > 5000:
                results.append(f"... (stopped after scanning {scanned} files)")
                break

            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                    if regex.search(line):
                        rel = str(f.relative_to(base)).replace("\\", "/")
                        results.append(f"{rel}:{i}:{line.strip()[:200]}")
                        count += 1
                        if count >= 100:
                            break
                if count >= 100:
                    break
            except Exception as exc:
                _logger.debug("Failed to read file during grep: %s: %s", f, exc, exc_info=True)
                log_tool_event("grep_read_failed", tool_name="search", message=f"Failed to read file during grep: {f}: {exc}")
                continue

        if not results:
            return ToolResult(
                output=f"No matches found for: {inp.pattern}",
                metadata={"pattern": inp.pattern, "matches": 0},
            )

        return ToolResult(
            title=f"Grep: {inp.pattern} → {count} matches",
            output="\n".join(results),
            summary=f"{count} matches",
            metadata={"pattern": inp.pattern, "matches": count},
        )
