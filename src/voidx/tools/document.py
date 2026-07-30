"""Read voidx built-in documents."""

from __future__ import annotations

import importlib.resources
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    drop_nullish_tool_fields,
    model_to_json_schema,
)

_DOCUMENTS_PACKAGE = "voidx.data"
_DOCUMENTS_ROOT = "documents"


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "read"] = Field(
        description="Document action: list reads a directory README index; read loads a Markdown document."
    )
    path: str | None = Field(
        default=None,
        description="POSIX-style relative path under the built-in document root; omit for the root index.",
    )


def _normalize_document_args(args):
    if not isinstance(args, dict):
        return args
    action = str(args.get("action") or "").strip().lower()
    if action in {"list", "read"}:
        return drop_nullish_tool_fields(args, "path")
    return args


class DocumentTool(BaseTool):
    id = "document"
    description = (
        'Read built-in documents only. action="list" reads a directory README index; '
        'action="read" loads a specific Markdown document. This tool does not read workspace files, '
        'generate documents, or search external sources. Start with action="list" when unsure what exists.'
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(DocumentInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_document_args(args)
        try:
            inp = DocumentInput.model_validate(args)
        except Exception as exc:
            return _error(f"Invalid arguments: {exc}")

        if inp.action == "list":
            return self._list(inp.path)
        return self._read(inp.path)

    def _list(self, path: str | None) -> ToolResult:
        if path and path.endswith(".md"):
            return _error("list requires a directory path")
        try:
            directory = _clean_relative_path(path) if path else ""
        except ValueError:
            return _error("invalid path")

        readme_path = f"{directory}/README.md" if directory else "README.md"
        try:
            content = _read_document_resource(readme_path)
        except FileNotFoundError:
            hint = 'Try document(action="list") to see available directories.'
            return _error(f"Document index not found: {directory or '/'} . {hint}")
        except (IsADirectoryError, TypeError, ValueError) as exc:
            return _error(f"Document index not available: {exc}")

        title_path = directory or "/"
        return ToolResult(
            title=f"Document index: {title_path}",
            output=content,
            summary=f"document index: {title_path}",
            metadata={
                "action": "list",
                "path": directory,
                "kind": "index",
                "directory": directory,
            },
        )

    def _read(self, path: str | None) -> ToolResult:
        if not path:
            return _error("read requires path")
        if not path.endswith(".md"):
            return _error("read requires a .md file path")
        try:
            safe_path = _clean_relative_path(path)
        except ValueError:
            return _error("invalid path")

        try:
            content = _read_document_resource(safe_path)
        except FileNotFoundError:
            return _error(
                'Document not found. Try document(action="list") or '
                'document(action="list", path="<dir>") first.'
            )
        except (IsADirectoryError, TypeError, ValueError) as exc:
            return _error(f"Document not available: {exc}")

        directory = str(PurePosixPath(safe_path).parent)
        if directory == ".":
            directory = ""
        return ToolResult(
            title=f"Document: {safe_path}",
            output=content,
            summary=f"document: {safe_path}",
            metadata={
                "action": "read",
                "path": safe_path,
                "kind": "document",
                "directory": directory,
            },
        )


def _clean_relative_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("invalid path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("invalid path")
    rel = PurePosixPath(path)
    if rel.is_absolute():
        raise ValueError("invalid path")
    return rel.as_posix()


def _read_document_resource(path: str) -> str:
    ref = importlib.resources.files(_DOCUMENTS_PACKAGE).joinpath(_DOCUMENTS_ROOT, path)
    return ref.read_text(encoding="utf-8")


def _error(output: str) -> ToolResult:
    return ToolResult(output=output, metadata={"error": True})
