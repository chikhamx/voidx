"""Tool authorization port."""

from typing import Protocol


class PermissionAuthorizer(Protocol):
    async def authorize(self, tool_name: str, arguments: dict) -> bool: ...
