"""Repo map tool — structural overview of the codebase for LLM context.

Extracts function/class signatures from source files using regex-based
parsing. Groups by file, limits output to a token budget (~4000 tokens).

Supports Python by default. Extensible to other languages.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult, resolve_safe

OUTPUT_TOKEN_BUDGET = 4000
DIR_ENTRY_LIMIT = 200

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".tox", ".eggs",
    ".idea", ".vscode", "dist", "build", "opencode",
    ".claude", ".ruff_cache",
}
SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".whl", ".egg",
}


class RepoMapInput(BaseModel):
    path: str | None = Field(
        default=None,
        description="Subdirectory to focus on. Defaults to workspace root."
    )
    detail: str = Field(
        default="overview",
        description="'overview' = top-level symbols only, 'signatures' = all function/class signatures"
    )
    pattern: str | None = Field(
        default=None,
        description="Glob pattern to filter files, e.g. '*.py' or 'src/**/*.ts'"
    )


class RepoMapTool(BaseTool):
    id = "repo_map"
    description = (
        "Structural map of the codebase: file tree with function/class signatures. "
        "detail='overview' returns top-level symbols only. detail='signatures' "
        "returns all symbols including methods. Narrow with path or pattern."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(RepoMapInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = RepoMapInput.model_validate(args)
        base = Path(ctx.workspace)
        if inp.path:
            root = resolve_safe(ctx.workspace, inp.path)
            if root is None:
                return ToolResult(output=f"Path traversal blocked: {inp.path}")
        else:
            root = base

        if not root.exists():
            return ToolResult(output=f"Path not found: {root}")

        files = _collect_files(root, inp.pattern)
        if not files:
            return ToolResult(output=f"No source files found in: {root}")

        detail = inp.detail
        entries: list[str] = []
        total_tokens = 0

        for f in files:
            rel = str(f.relative_to(base)).replace("\\", "/")

            if detail == "overview":
                symbols = _extract_top_level(f)
            else:
                symbols = _extract_all_symbols(f)

            if symbols:
                entry = f"{rel}:\n" + "".join(symbols)
            else:
                entry = f"{rel}: (no symbols)  " if detail == "signatures" else ""

            est = _estimate_tokens(entry)
            if total_tokens + est > OUTPUT_TOKEN_BUDGET:
                remaining = len(files) - len(entries)
                entries.append(f"\n... ({remaining} more files omitted — token limit)")
                break

            if entry:
                entries.append(entry)
                total_tokens += est

        if not entries:
            return ToolResult(output=f"No recognizable symbols found in: {root}")

        return ToolResult(
            title=f"Repo map: {root.relative_to(base)} ({len(entries)} files)",
            output="\n".join(entries),
            metadata={
                "root": str(root.relative_to(base)),
                "files": len(entries),
                "detail": detail,
            },
        )


# ── file collection ──────────────────────────────────────────────────────────

def _collect_files(root: Path, pattern: str | None) -> list[Path]:
    files: list[Path] = []

    def _scan(dir_path: Path):
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return
        for entry in entries:
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_dir():
                _scan(entry)
            elif entry.is_file():
                if entry.suffix in SKIP_SUFFIXES:
                    continue
                if pattern and not entry.match(pattern):
                    continue
                if _is_source(entry):
                    files.append(entry)

    _scan(root)
    return files


def _is_source(path: Path) -> bool:
    ext_map = {
        ".py": True, ".ts": True, ".tsx": True, ".js": True, ".jsx": True,
        ".go": True, ".rs": True, ".java": True, ".kt": True, ".swift": True,
        ".c": True, ".h": True, ".cpp": True, ".hpp": True, ".rb": True,
        ".php": True, ".css": True, ".scss": True, ".less": True,
        ".sql": True, ".sh": True, ".bash": True, ".zsh": True,
        ".yaml": True, ".yml": True, ".toml": True, ".json": True,
        ".md": True, ".txt": True, ".cfg": True, ".ini": True,
        ".dockerfile": True, ".makefile": True, ".mk": True,
    }
    name = path.name.lower()
    if name == "dockerfile" or name == "makefile":
        return True
    return ext_map.get(path.suffix, False)


# ── Python symbol extraction ─────────────────────────────────────────────────


def _extract_all_symbols(f: Path) -> list[str]:
    if f.suffix == ".py":
        return _extract_python_symbols(f)
    return _extract_generic(f)


def _extract_top_level(f: Path) -> list[str]:
    if f.suffix == ".py":
        return _extract_python_symbols(f, top_level_only=True)
    return _extract_generic(f)


def _extract_python_symbols(f: Path, top_level_only: bool = False) -> list[str]:
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines: list[str] = []
    decorator: str | None = None

    _RE_COMBINED = re.compile(
        r"^(\s*)class\s+(\w+)\s*(?:\(([^)]*?)\))?\s*:"
        r"|^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)\s*(?:->[^:]+?)?\s*:"
        r"|^[ \t]*@(\w+)",
        re.MULTILINE,
    )

    for match in _RE_COMBINED.finditer(text):
        indent = match.group(1) or match.group(4) or ""
        level = len(indent) // 4 if indent else 0

        if match.group(0).startswith("@"):
            decorator = match.group(7) or ""
            continue

        is_class = match.group(2) is not None
        func_name = match.group(5)
        name = match.group(2) or func_name
        args = match.group(3) or match.group(6) or ""

        if top_level_only and level > 0:
            continue

        prefix = "  " * level
        if is_class:
            tag = ""
            if decorator:
                tag = f"@{decorator}\n{prefix}"
                decorator = None
            base = f"({match.group(3)})" if match.group(3) else ""
            lines.append(f"{prefix}{tag}class {name}{base}\n")
        else:
            tag = ""
            if decorator:
                tag = f"@{decorator} "
                decorator = None
            if match.group(0).lstrip().startswith("async"):
                tag = "async "
            lines.append(f"{prefix}{tag}def {name}({args})\n")

    return lines


def _extract_generic(f: Path) -> list[str]:
    """Minimal extraction for non-Python files: just report the file."""
    return []


# ── token estimation ─────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Quick token estimate: ~4 chars per token for code."""
    return max(1, len(text) // 4)
