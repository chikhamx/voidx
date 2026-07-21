"""Static Chat tool binding and resource scope policy."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


_LOCAL_READ_TOOLS = frozenset({"read", "glob", "grep", "lsp"})
_ALWAYS_BOUND_TOOLS = frozenset({"websearch", "webfetch", "mcp"})
_ESCAPE_TOOLS = frozenset(
    {"bash", "powershell", "write", "manage", "replace", "git", "agent", "subagent"}
)


class ChatResourceScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace: Path | None = None

    @field_validator("workspace")
    @classmethod
    def normalize_workspace(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None


class ChatToolDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    requests_approval: bool = False


class ChatToolView(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: ChatResourceScope
    bound_tool_ids: frozenset[str] = Field(default_factory=frozenset)

    @classmethod
    def for_scope(cls, scope: ChatResourceScope) -> "ChatToolView":
        tool_ids = set(_ALWAYS_BOUND_TOOLS)
        if scope.workspace is not None:
            tool_ids.update(_LOCAL_READ_TOOLS)
        return cls(scope=scope, bound_tool_ids=frozenset(tool_ids))

    def check(self, tool_id: str, *, path: Path | None = None) -> ChatToolDecision:
        if tool_id in _ESCAPE_TOOLS:
            return ChatToolDecision(allowed=False, reason="tool_not_bound")
        if tool_id.startswith("mcp:"):
            return ChatToolDecision(allowed=True, reason="mcp_tool_bound")
        if tool_id not in self.bound_tool_ids:
            return ChatToolDecision(allowed=False, reason="tool_not_bound")
        if path is not None and not self._path_is_in_scope(path):
            return ChatToolDecision(allowed=False, reason="resource_out_of_scope")
        return ChatToolDecision(allowed=True, reason="tool_bound")

    def allows(self, tool_id: str, *, path: Path | None = None) -> bool:
        return self.check(tool_id, path=path).allowed

    def _path_is_in_scope(self, path: Path) -> bool:
        if self.scope.workspace is None:
            return False
        try:
            path.expanduser().resolve().relative_to(self.scope.workspace)
        except ValueError:
            return False
        return True
