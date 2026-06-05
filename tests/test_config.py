import pytest
import json
import sys

sys.path.insert(0, "src")

from voidx.config import CodeIde, ApprovalPolicy, ApprovalReviewer, McpServerConfig, PermissionMode, Profile, SandboxMode, Settings, WebToolRoute
from voidx.memory.model_profiles import delete_model_profile_async


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
        args=["-m", "voidx.mcp_servers.web"],
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


def test_settings_tracks_skill_enable_disable(tmp_path):
    settings = Settings(str(tmp_path))

    assert settings.set_skill_enabled("docs", False) == tmp_path / ".voidx" / "skills.json"
    assert settings.set_skill_enabled("python", True) == tmp_path / ".voidx" / "skills.json"

    selection = Settings(str(tmp_path)).get_skill_selection()
    saved = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))

    assert saved == {"version": 1, "enabled": ["python"], "disabled": ["docs"]}
    if (tmp_path / ".voidx" / "settings.json").exists():
        assert "skills" not in json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert selection.disabled == {"docs"}
    assert selection.enabled == {"python"}

    settings.set_skill_enabled("docs", True)
    selection = Settings(str(tmp_path)).get_skill_selection()
    saved = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))

    assert saved == {"version": 1, "enabled": ["docs", "python"], "disabled": []}
    assert selection.disabled == set()
    assert selection.enabled == {"docs", "python"}


def test_settings_reads_legacy_skill_selection_from_voidx_json(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({"skills": {"enabled": ["docs"], "disabled": ["python"]}}),
        encoding="utf-8",
    )

    selection = Settings(str(tmp_path)).get_skill_selection()

    assert selection.enabled == {"docs"}
    assert selection.disabled == {"python"}


async def test_permission_mode_presets_drive_build_config(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_sandbox_workspace_write([str(tmp_path / "external")])

    settings.set_permission_mode(PermissionMode.FULL_ACCESS)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.FULL_ACCESS
    assert cfg.sandbox_mode == SandboxMode.DANGER_FULL_ACCESS
    assert cfg.approval_policy == ApprovalPolicy.NEVER
    assert cfg.approval_reviewer == ApprovalReviewer.USER
    assert cfg.sandbox_workspace_write == []

    settings.set_permission_mode(PermissionMode.AUTO_REVIEW)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.AUTO_REVIEW
    assert cfg.sandbox_mode == SandboxMode.WORKSPACE_WRITE
    assert cfg.approval_policy == ApprovalPolicy.UNTRUSTED
    assert cfg.approval_reviewer == ApprovalReviewer.AUTO_REVIEW

    settings.set_permission_mode(PermissionMode.READ_ONLY)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.sandbox_mode == SandboxMode.READ_ONLY
    assert cfg.approval_policy == ApprovalPolicy.UNTRUSTED


async def test_build_config_defaults_and_reads_ask_compact(tmp_path):
    assert (await (await Settings.create(str(tmp_path))).build_config()).ask_compact is False

    (tmp_path / "voidx.json").write_text(
        json.dumps({"askCompact": True}),
        encoding="utf-8",
    )

    assert (await (await Settings.create(str(tmp_path))).build_config()).ask_compact is True


async def test_low_level_permission_changes_mark_custom_mode(tmp_path):
    settings = Settings(str(tmp_path))

    settings.set_permission_mode(PermissionMode.DEFAULT)
    settings.set_approval_policy(ApprovalPolicy.ON_FAILURE)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.CUSTOM
    assert cfg.sandbox_mode == SandboxMode.WORKSPACE_WRITE
    assert cfg.approval_policy == ApprovalPolicy.ON_FAILURE


def test_settings_defaults_and_saves_code_ide(tmp_path):
    settings = Settings(str(tmp_path))

    assert settings.get_code_ide() == CodeIde.TRAE

    settings.set_code_ide(CodeIde.GHOSTTY)

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["codeIde"] == "ghostty"
    assert Settings(str(tmp_path)).get_code_ide() == CodeIde.GHOSTTY


def test_settings_migrates_legacy_skill_selection_on_write(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({"skills": {"enabled": ["legacy"], "disabled": ["docs"]}}),
        encoding="utf-8",
    )

    settings = Settings(str(tmp_path))
    settings.set_skill_enabled("docs", True)
    settings.set_tavily_api_key("tvly-test")

    state = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))
    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))

    assert state == {"version": 1, "enabled": ["docs", "legacy"], "disabled": []}
    assert saved == {"tavily_api_key": "tvly-test"}


def test_settings_prefers_skill_state_file_over_legacy_voidx_json(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({"skills": {"enabled": ["legacy"], "disabled": []}}),
        encoding="utf-8",
    )
    state_path = tmp_path / ".voidx" / "skills.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"version": 1, "enabled": ["docs"], "disabled": ["python"]}),
        encoding="utf-8",
    )

    selection = Settings(str(tmp_path)).get_skill_selection()

    assert selection.enabled == {"docs"}
    assert selection.disabled == {"python"}


async def test_build_config_uses_default_reasoning_effort(tmp_path):
    profile_name = f"mimo/{tmp_path.name}-v2.5"
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "default_profile": profile_name,
            "custom_providers": {
                "mimo": {
                    "protocol": "openai",
                    "base_url": "https://mimo.example/v1",
                },
            },
            "custom_models": {
                "mimo": ["legacy-custom-model"],
            },
            "profiles": {
                profile_name: {
                    "api_key": "sk-test",
                },
            },
        }),
        encoding="utf-8",
    )

    try:
        settings = await Settings.create(str(tmp_path))
        cfg = await settings.build_config()

        assert cfg.model.provider == "mimo"
        assert cfg.model.model == f"{tmp_path.name}-v2.5"
        assert cfg.model.base_url == "https://mimo.example/v1"
        assert cfg.model.protocol == "openai"
        assert cfg.model.reasoning_effort == "xhigh"
        saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert saved == {"current_profile": profile_name}
    finally:
        await delete_model_profile_async(profile_name)


async def test_save_profile_persists_model_in_db_and_only_current_profile_in_json(tmp_path):
    profile_name = f"custom/{tmp_path.name}-model"
    settings = Settings(str(tmp_path))
    profile = Profile(
        name=profile_name,
        api_key="sk-custom",
        base_url="https://custom.example/v1",
        protocol="openai",
    )

    try:
        await settings.save_profile(profile)
        settings.add_custom_model("custom", "another-model")
        settings.add_custom_provider(
            "another-provider",
            protocol="openai",
            base_url="https://another.example/v1",
        )

        saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert saved == {"current_profile": profile_name}

        loaded = await settings.resolve_profile(profile_name)
        assert loaded == profile
    finally:
        await delete_model_profile_async(profile_name)
