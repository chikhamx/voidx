"""File operation tools — read, manage, write, replace."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ManageTool": ("manage", "ManageTool"),
    "ManageInput": ("manage", "ManageInput"),
    "MoveSpec": ("manage", "MoveSpec"),
    "WriteTool": ("write", "WriteTool"),
    "WriteInput": ("write", "WriteInput"),
    "FileReadTool": ("read", "FileReadTool"),
    "FileReadInput": ("read", "FileReadInput"),
    "FileReplaceTool": ("replace", "FileReplaceTool"),
    "FileReplaceInput": ("replace", "FileReplaceInput"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name, attribute = _EXPORTS[name]
    return getattr(import_module(f"{__name__}.{module_name}"), attribute)
