"""Git tool data models and exceptions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class GitInput(BaseModel):
    path: str = Field(
        default="",
        description="Repository working directory. Relative paths resolve from the workspace; empty uses the workspace root.",
    )
    args: str = Field(
        min_length=1,
        description='raw git subcommand and arguments only. Do not include git itself; do not include the git executable, e.g. "status --porcelain" or "log --oneline -5".',
    )

    @model_validator(mode="before")
    @classmethod
    def accept_command_alias(cls, data):
        if isinstance(data, dict) and not str(data.get("args") or "").strip():
            command = data.get("command")
            if command is not None:
                return {**data, "args": command}
        return data


class GitRepo(BaseModel):
    repo_root: str
    workspace: str


class GitProcessTimeout(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(result.get("stderr") or "git command timed out")
