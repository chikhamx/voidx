import pytest
import json
import sys
from pathlib import Path


from voidx.config import (
    CodeIde,
    McpServerConfig,
    ParallelSubagentsConfig,
    Profile,
    Settings,
    UserProfile,
    WebToolRoute,
)
import voidx.memory.store as store
from voidx.memory.model_profiles import delete_model_profile_async


def _set_home(monkeypatch, path: Path) -> None:
    monkeypatch.setattr("voidx.config.settings._settings_home", lambda: path)


@pytest.fixture(autouse=True)
def isolated_global_store(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    previous_data_dir = store.DATA_DIR
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = previous_data_dir


def test_lsp_format_after_edit_defaults_true_and_is_workspace_scoped(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(str(workspace))

    assert settings.get_lsp_format_after_edit() is True
    saved = settings.set_lsp_format_after_edit(False)

    assert saved == workspace / ".voidx" / "settings.json"
    assert settings.get_lsp_format_after_edit() is False
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["lsp"]["format_after_edit"] is False


def test_lsp_format_after_edit_ignores_invalid_value(tmp_path):
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".voidx"
    config_dir.mkdir(parents=True)
    (config_dir / "settings.json").write_text(
        json.dumps({"lsp": {"format_after_edit": "nope"}}),
        encoding="utf-8",
    )

    assert Settings(str(workspace)).get_lsp_format_after_edit() is True


async def test_settings_reads_global_values_before_workspace_overrides(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    global_dir = tmp_path / ".voidx"
    global_dir.mkdir()
    (global_dir / "settings.json").write_text(
        json.dumps({
            "codeIde": "ghostty",
            "current_profile": "global/provider",
            "tavily_api_key": "global-key",
            "update_check": {"enabled": False},
            "parallel_subagents": {"enabled": True, "max_concurrent": 7},
        }),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".voidx").mkdir()
    (workspace / ".voidx" / "settings.json").write_text(
        json.dumps({
            "current_profile": "workspace/provider",
            "userProfile": {"language": "zh-CN"},
        }),
        encoding="utf-8",
    )

    settings = Settings(str(workspace))

    assert settings.get_code_ide() == CodeIde.GHOSTTY
    assert settings.get_tavily_api_key() == "global-key"
    assert settings.get_update_check_enabled() is False
    assert settings.get_parallel_subagents() == ParallelSubagentsConfig(enabled=True, max_concurrent=7)
    assert settings.get_user_profile() == UserProfile(language="zh-CN")


def test_settings_workspace_overrides_global_mcp_servers(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    global_dir = tmp_path / ".voidx"
    global_dir.mkdir()
    (global_dir / "settings.json").write_text(
        json.dumps({
            "mcpServers": {
                "tavily": {"command": "npx", "disabled": False, "tools": ["search"]},
                "global-only": {"command": "node", "disabled": False},
            }
        }),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".voidx").mkdir()
    (workspace / ".voidx" / "settings.json").write_text(
        json.dumps({
            "mcpServers": {
                "tavily": {"disabled": True},
            }
        }),
        encoding="utf-8",
    )

    servers = Settings(str(workspace)).list_mcp_servers()

    assert {server.name for server in servers} == {"tavily", "global-only"}
    assert next(server for server in servers if server.name == "tavily").disabled is True


def test_delete_inherited_global_mcp_server_writes_workspace_disable(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    global_dir = tmp_path / ".voidx"
    global_dir.mkdir()
    (global_dir / "settings.json").write_text(
        json.dumps({
            "mcpServers": {
                "global-only": {"command": "node", "args": ["server.js"]},
            }
        }),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".voidx").mkdir()
    workspace_settings_path = workspace / ".voidx" / "settings.json"
    workspace_settings_path.write_text(
        json.dumps({
            "mcpServers": {
                "workspace-only": {"command": "python"},
            }
        }),
        encoding="utf-8",
    )

    settings = Settings(str(workspace))
    path = settings.delete_mcp_server("global-only")

    workspace_saved = json.loads(workspace_settings_path.read_text(encoding="utf-8"))
    global_saved = json.loads((global_dir / "settings.json").read_text(encoding="utf-8"))

    assert path == workspace_settings_path
    assert settings.get_mcp_server("global-only") is not None
    assert settings.get_mcp_server("global-only").disabled is True
    assert workspace_saved["mcpServers"]["global-only"] == {"disabled": True}
    assert global_saved["mcpServers"]["global-only"] == {"command": "node", "args": ["server.js"]}


def test_mapping_setters_do_not_copy_global_entries_into_workspace_overrides(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    global_dir = tmp_path / ".voidx"
    global_dir.mkdir()
    (global_dir / "settings.json").write_text(
        json.dumps({
            "mcpServers": {
                "global-only": {"command": "node", "args": ["global.js"]},
            },
            "web": {
                "fetch": {
                    "backend": "mcp",
                    "server": "global-only",
                    "tool": "web_fetch",
                },
            },
        }),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".voidx").mkdir()
    (workspace / ".voidx" / "settings.json").write_text(
        json.dumps({
            "mcpServers": {
                "workspace-only": {"command": "python"},
            },
            "web": {
                "search": {"backend": "legacy", "server": None, "tool": None},
            },
        }),
        encoding="utf-8",
    )

    settings = Settings(str(workspace))
    settings.save_mcp_server(McpServerConfig(name="new-local", command="npx"))
    settings.set_mcp_server_disabled("global-only", True)
    settings.set_web_tool_route(
        "search",
        WebToolRoute(backend="mcp", server="new-local", tool="web_search"),
    )

    workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    global_saved = json.loads((global_dir / "settings.json").read_text(encoding="utf-8"))

    assert set(workspace_saved["mcpServers"]) == {"workspace-only", "new-local", "global-only"}
    assert workspace_saved["mcpServers"]["workspace-only"] == {"command": "python"}
    assert workspace_saved["mcpServers"]["new-local"]["command"] == "npx"
    assert workspace_saved["mcpServers"]["global-only"] == {"disabled": True}
    assert workspace_saved["web"] == {
        "search": {
            "backend": "mcp",
            "server": "new-local",
            "tool": "web_search",
        },
    }
    assert global_saved["mcpServers"] == {
        "global-only": {"command": "node", "args": ["global.js"]},
    }
    assert global_saved["web"]["fetch"]["server"] == "global-only"


async def test_global_setters_write_global_file_when_workspace_has_no_override(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(str(workspace))

    settings.set_code_ide(CodeIde.GHOSTTY)
    settings.mark_update_check("9.0.0", now=1000)

    global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert global_saved["codeIde"] == "ghostty"
    assert global_saved["update_check"]["last_latest_version"] == "9.0.0"
    assert not (workspace / ".voidx" / "settings.json").exists()


async def test_custom_providers_are_legacy_no_ops(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    settings = Settings(str(tmp_path))

    settings.add_custom_provider("legacy", protocol="openai", base_url="https://legacy.example")
    settings.add_custom_model("legacy", "legacy-model")

    assert settings.list_custom_providers() == []
    assert await settings.list_custom_models("legacy") == []


def test_settings_lists_mcp_servers_from_voidx_json(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "mcpServers": {
                "web-reader": {
                    "command": "npx",
                    "args": ["web-reader"],
                    "tools": ["read_url"],
                },
                "disabled-server": {
                    "command": "node",
                    "disabled": True,
                    "tools": {"inspect": {}},
                },
            }
        }),
        encoding="utf-8",
    )

    servers = Settings(str(tmp_path)).list_mcp_servers()

    assert [server.name for server in servers] == ["web-reader", "disabled-server"]
    assert servers[0].command == "npx"
    assert servers[0].tool_count == 1
    assert servers[1].disabled is True
    assert servers[1].tool_count == 1


def test_settings_saves_mcp_server_and_web_routes(tmp_path):
    settings = Settings(str(tmp_path))

    settings.save_mcp_server(McpServerConfig(
        name="voidx-web",
        command="python",
        args=["-m", "voidx.mcp.server.web"],
        tools=["web_search", "web_fetch"],
    ))
    settings.set_web_tool_route(
        "search",
        WebToolRoute(backend="mcp", server="voidx-web", tool="web_search"),
    )

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["voidx-web"]["command"] == "python"
    assert "name" not in saved["mcpServers"]["voidx-web"]
    assert saved["web"]["search"]["server"] == "voidx-web"

    loaded = Settings(str(tmp_path))
    assert loaded.get_mcp_server("voidx-web").tool_count == 2
    assert loaded.get_web_tool_route("search").tool == "web_search"

    loaded.delete_mcp_server("voidx-web")
    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert "voidx-web" not in saved["mcpServers"]
    assert saved["web"]["search"]["backend"] == "legacy"


def test_settings_set_mcp_server_disabled_clears_web_routes(tmp_path):
    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(
        name="voidx-web",
        command="python",
        args=["-m", "voidx.mcp.server.web"],
        tools=["web_search", "web_fetch"],
    ))
    settings.set_web_tool_route(
        "search",
        WebToolRoute(backend="mcp", server="voidx-web", tool="web_search"),
    )
    settings.set_web_tool_route(
        "fetch",
        WebToolRoute(backend="mcp", server="voidx-web", tool="web_fetch"),
    )

    settings.set_mcp_server_disabled("voidx-web", True)

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["voidx-web"]["disabled"] is True
    assert saved["web"]["search"]["backend"] == "legacy"
    assert saved["web"]["fetch"]["backend"] == "legacy"

    settings.set_mcp_server_disabled("voidx-web", False)

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["voidx-web"]["disabled"] is False
    assert saved["web"]["search"]["backend"] == "legacy"


def test_mcp_server_config_effective_transport():
    # stdio: no url, no explicit transport
    stdio = McpServerConfig(name="test", command="npx")
    assert stdio.effective_transport == "stdio"

    # sse: has url, no explicit transport
    sse = McpServerConfig(name="test", url="https://mcp.example.com/sse")
    assert sse.effective_transport == "sse"

    # explicit transport overrides auto-detect
    explicit = McpServerConfig(name="test", url="https://mcp.example.com/sse", transport="stdio")
    assert explicit.effective_transport == "stdio"


def test_settings_saves_mcp_server_with_url(tmp_path):
    settings = Settings(str(tmp_path))

    settings.save_mcp_server(McpServerConfig(
        name="remote",
        url="https://mcp.example.com/sse",
        env={"API_KEY": "secret"},
    ))

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["remote"]["url"] == "https://mcp.example.com/sse"
    assert saved["mcpServers"]["remote"]["env"] == {"API_KEY": "secret"}

    loaded = Settings(str(tmp_path))
    server = loaded.get_mcp_server("remote")
    assert server.url == "https://mcp.example.com/sse"
    assert server.effective_transport == "sse"


