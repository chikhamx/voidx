"""MCP client shim — placeholder for Rust-backed MCP client."""


class McpClient:
    """Minimal MCP client stub. Full implementation in Rust via voidx_core."""

    def __init__(self, name: str, command: str, args: list[str] | None = None):
        self.name = name
        self.command = command
        self.args = args or []

    async def start(self):
        pass

    async def list_tools(self) -> list[dict]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        return {"result": "MCP tools not yet bridged to Rust"}
