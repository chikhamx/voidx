"""Configuration-only MCP auto discovery injected into the stable system prefix."""

import json

import pytest

from voidx.config import Settings
from voidx.agent.application.instruction import InstructionService
from voidx.mcp.auto import render_available_mcp_servers


def _write_settings(tmp_path, servers: dict) -> None:
    (tmp_path / "voidx.json").write_text(
        json.dumps({"mcpServers": servers}),
        encoding="utf-8",
    )


def test_renderer_includes_only_enabled_auto_servers_in_stable_order(tmp_path):
    _write_settings(tmp_path, {
        "zeta": {"command": "z", "auto": True, "description": "Zeta tools", "source": "workspace"},
        "manual": {"command": "m", "description": "Manual tools"},
        "disabled": {"command": "d", "auto": True, "disabled": True},
        "alpha": {"command": "a", "auto": True, "source": "bundled"},
        "tools-only": {"command": "t", "auto": True, "tools": ["search", "extract", "crawl", "rank"]},
    })

    section = render_available_mcp_servers(Settings(str(tmp_path)))

    assert section.startswith("## Available MCP Servers")
    assert 'mcp(op="load", server="<name>")' in section
    assert section.index("alpha") < section.index("zeta")
    assert "- alpha: No description configured." in section
    assert "- tools-only: Configured tools: search, extract, crawl, ..." in section
    assert "- zeta: Zeta tools" in section
    assert "[auto]" not in section
    assert "source:" not in section
    assert "manual" not in section
    assert "disabled" not in section


def test_renderer_prefers_generated_descriptions(tmp_path):
    _write_settings(tmp_path, {
        "tavily": {"command": "npx", "auto": True},
    })

    section = render_available_mcp_servers(
        Settings(str(tmp_path)),
        descriptions={"tavily": "Search the web for current information."},
    )

    assert "- tavily: Search the web for current information." in section
    assert "No description configured" not in section


@pytest.mark.asyncio
async def test_instruction_system_includes_configuration_only_auto_section(tmp_path):

    _write_settings(tmp_path, {
        "tavily": {
            "command": "npx",
            "auto": True,
            "description": "Web research",
            "source": "workspace",
            "tools": ["search", "extract"],
        },
        "manual": {"command": "node", "description": "Manual server"},
    })
    service = InstructionService(str(tmp_path), settings=Settings(str(tmp_path)))

    joined = "\n\n".join(await service.system())

    assert "## Available MCP Servers" in joined
    assert "- tavily: Web research" in joined
    assert "manual" not in joined
    assert "- search" not in joined
    assert "- extract" not in joined
    assert "connected" not in joined
    assert "tool_count" not in joined
    assert "source:" not in joined


@pytest.mark.asyncio
async def test_instruction_system_uses_manager_generated_descriptions(tmp_path):
    _write_settings(tmp_path, {
        "tavily": {"command": "npx", "auto": True},
    })
    service = InstructionService(str(tmp_path), settings=Settings(str(tmp_path)))
    service.set_mcp_description_provider(
        lambda: {"tavily": "Search the web for current information."}
    )

    joined = "\n\n".join(await service.system())

    assert "- tavily: Search the web for current information." in joined


@pytest.mark.asyncio
async def test_auto_section_is_frozen_for_instruction_service_session(tmp_path):

    _write_settings(tmp_path, {
        "tavily": {"command": "npx", "auto": True, "description": "Original"},
    })
    service = InstructionService(str(tmp_path), settings=Settings(str(tmp_path)))
    first = "\n\n".join(await service.system())

    _write_settings(tmp_path, {
        "tavily": {"command": "npx", "auto": True, "description": "Changed"},
        "github": {"command": "node", "auto": True},
    })
    second = "\n\n".join(await service.system())

    assert "Original" in first
    assert "Original" in second
    assert "Changed" not in second
    assert "github" not in second
