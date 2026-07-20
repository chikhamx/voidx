"""The fixed `mcp` gateway tool is always registered alongside McpManager."""

import json

from voidx.agent.infrastructure.langgraph.runtime.wiring import build_external_managers
from voidx.config import Settings
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry


def _registry_with_mcp(tmp_path) -> ToolRegistry:
    settings = Settings(str(tmp_path))
    registry = ToolRegistry(settings=settings)
    build_external_managers(
        settings=settings,
        tools=registry,
        permission=PermissionService(),
        workspace=str(tmp_path),
    )
    return registry


def test_gateway_tool_registered_with_mcp_manager(tmp_path):
    registry = _registry_with_mcp(tmp_path)
    tool_names = [t["function"]["name"] for t in registry.tools_for_llm()]
    assert "mcp" in tool_names


def test_gateway_tool_schema_is_stable_and_catalog_free(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "mcpServers": {
                "tavily": {"command": "echo", "disabled": True},
                "github": {"command": "echo", "disabled": True},
            },
        }),
        encoding="utf-8",
    )
    registry = _registry_with_mcp(tmp_path)

    mcp_defs = [t for t in registry.tools_for_llm() if t["function"]["name"] == "mcp"]
    assert len(mcp_defs) == 1
    serialized = json.dumps(mcp_defs[0])
    assert "tavily" not in serialized
    assert "github" not in serialized
