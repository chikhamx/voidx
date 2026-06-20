"""File operation tools — read, write, edit. Deterministic, typed I/O."""

from __future__ import annotations

from .edit_execute import (
    FileDeleteInput,
    FileDeleteTool,
    FileEditInput,
    FileEditTool,
    FileInsertInput,
    FileInsertTool,
    FileReplaceInput,
    FileReplaceTool,
)
from .edit_resolve import _find_paragraph
from .read import FileReadInput, FileReadTool
from .types import EditEntry
from .write import FileWriteInput, FileWriteTool

__all__ = [
    "FileDeleteTool",
    "FileDeleteInput",
    "FileReadTool",
    "FileReadInput",
    "FileWriteTool",
    "FileWriteInput",
    "FileEditTool",
    "FileEditInput",
    "FileInsertTool",
    "FileInsertInput",
    "FileReplaceTool",
    "FileReplaceInput",
    "EditEntry",
    "_find_paragraph",
]
