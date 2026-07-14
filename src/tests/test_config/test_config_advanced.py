import pytest
import json
import sys
from pathlib import Path


from voidx.config import (
    CodeIde,
    McpServerConfig,
    ParallelSubagentsConfig,
    PermissionMode,
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


def test_settings_tracks_skill_enable_disable(tmp_path):
    settings = Settings(str(tmp_path))

    assert settings.set_skill_enabled("docs", False) == tmp_path / ".voidx" / "skills.json"
    assert settings.set_skill_enabled("python", True) == tmp_path / ".voidx" / "skills.json"

    selection = Settings(str(tmp_path)).get_skill_selection()
    saved = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))

    assert saved == {"version": 2, "enabled": ["python"], "disabled": ["docs"], "auto": []}
    if (tmp_path / ".voidx" / "settings.json").exists():
        assert "skills" not in json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert selection.disabled == {"docs"}
    assert selection.enabled == {"python"}
    assert selection.auto == set()

    settings.set_skill_enabled("docs", True)
    selection = Settings(str(tmp_path)).get_skill_selection()
    saved = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))

    assert saved == {"version": 2, "enabled": ["docs", "python"], "disabled": [], "auto": []}
    assert selection.disabled == set()
    assert selection.enabled == {"docs", "python"}
    assert selection.auto == set()

    settings.set_skill_auto("docs", True)
    selection = Settings(str(tmp_path)).get_skill_selection()
    saved = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))

    assert saved == {"version": 2, "enabled": ["docs", "python"], "disabled": [], "auto": ["docs"]}
    assert selection.auto == {"docs"}

    settings.set_skill_enabled("docs", False)
    selection = Settings(str(tmp_path)).get_skill_selection()
    saved = json.loads((tmp_path / ".voidx" / "skills.json").read_text(encoding="utf-8"))

    assert saved == {"version": 2, "enabled": ["python"], "disabled": ["docs"], "auto": []}
    assert selection.auto == set()


def test_settings_reads_legacy_skill_selection_from_voidx_json(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({"skills": {"enabled": ["docs"], "disabled": ["python"]}}),
        encoding="utf-8",
    )

    selection = Settings(str(tmp_path)).get_skill_selection()

    assert selection.enabled == {"docs"}
    assert selection.disabled == {"python"}
    assert selection.auto == set()


async def test_permission_mode_drives_build_config_without_rewriting_boundaries(tmp_path):
    settings = Settings(str(tmp_path))
    external = str(tmp_path / "external")
    settings.set_sandbox_writable_dirs([external])

    settings.set_permission_mode(PermissionMode.FULL_ACCESS)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.FULL_ACCESS
    assert cfg.sandbox_writable_dirs == [external]

    settings.set_permission_mode(PermissionMode.PROJECT_TRUSTED)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.PROJECT_TRUSTED
    assert cfg.sandbox_writable_dirs == [external]

    settings.set_permission_mode(PermissionMode.READ_ONLY)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.READ_ONLY
    assert cfg.sandbox_writable_dirs == [external]




def test_legacy_permission_schema_migrates_to_canonical_fields(tmp_path):
    external = tmp_path / "external"
    (tmp_path / "voidx.json").write_text(
        json.dumps({"sandbox_workspace_write": [str(external)]}),
        encoding="utf-8",
    )

    settings = Settings(str(tmp_path))

    assert settings.get_sandbox_writable_dirs() == [str(external)]
    assert settings.get_sandbox_readable_files() == []
    assert settings.get_sandbox_readable_dirs() == []
    assert settings.get_sandbox_writable_files() == []
    assert "sandbox_workspace_write" not in settings._effective_data()


async def test_build_config_uses_canonical_permission_grants(tmp_path):
    readable = tmp_path / "readable.txt"
    writable = tmp_path / "writable.txt"
    settings = Settings(str(tmp_path))
    settings.set_sandbox_readable_files([str(readable)])
    settings.set_sandbox_writable_files([str(writable)])

    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.sandbox_readable_files == [str(readable)]
    assert cfg.sandbox_writable_files == [str(writable)]
    assert not hasattr(cfg, "sandbox_workspace_write")


def test_mixed_permission_schema_prefers_canonical(tmp_path):
    legacy = tmp_path / "legacy"
    canonical = tmp_path / "canonical"
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "sandbox_workspace_write": [str(legacy)],
            "sandbox_writable_dirs": [str(canonical)],
            "sandbox_readable_files": [str(tmp_path / "readable.txt")],
        }),
        encoding="utf-8",
    )

    settings = Settings(str(tmp_path))

    assert settings.get_sandbox_writable_dirs() == [str(canonical)]
    assert str(legacy) not in settings.get_sandbox_writable_dirs()
    assert settings.get_sandbox_readable_files() == [str(tmp_path / "readable.txt")]
    assert "sandbox_workspace_write" not in settings._effective_data()


def test_legacy_migration_failure_fails_closed(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({"sandbox_workspace_write": [str(tmp_path / "valid"), 123]}),
        encoding="utf-8",
    )

    settings = Settings(str(tmp_path))

    assert settings.get_sandbox_readable_files() == []
    assert settings.get_sandbox_readable_dirs() == []
    assert settings.get_sandbox_writable_files() == []
    assert settings.get_sandbox_writable_dirs() == []
    assert "sandbox_workspace_write" not in settings._effective_data()


def test_permission_mode_preserves_path_grants(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_sandbox_readable_files([str(tmp_path / "readable-file")])
    settings.set_sandbox_readable_dirs([str(tmp_path / "readable-dir")])
    settings.set_sandbox_writable_files([str(tmp_path / "writable-file")])
    settings.set_sandbox_writable_dirs([str(tmp_path / "writable-dir")])

    settings.set_permission_mode(PermissionMode.PROJECT_TRUSTED)
    loaded = Settings(str(tmp_path))

    assert loaded.get_sandbox_readable_files() == [str(tmp_path / "readable-file")]
    assert loaded.get_sandbox_readable_dirs() == [str(tmp_path / "readable-dir")]
    assert loaded.get_sandbox_writable_files() == [str(tmp_path / "writable-file")]
    assert loaded.get_sandbox_writable_dirs() == [str(tmp_path / "writable-dir")]
    assert loaded.get_permission_mode() == PermissionMode.PROJECT_TRUSTED

async def test_build_config_defaults_and_reads_ask_compact(tmp_path):
    cfg = await (await Settings.create(str(tmp_path))).build_config()
    assert cfg.ask_compact is False
    assert cfg.parallel_subagents == ParallelSubagentsConfig()

    (tmp_path / "voidx.json").write_text(
        json.dumps({"askCompact": True}),
        encoding="utf-8",
    )

    assert (await (await Settings.create(str(tmp_path))).build_config()).ask_compact is True


async def test_build_config_reads_context_window(tmp_path):
    """build_config 从配置文件读 context_window 键注入 ModelConfig。"""
    cfg = await (await Settings.create(str(tmp_path))).build_config()
    assert cfg.model.context_window is None

    (tmp_path / "voidx.json").write_text(
        json.dumps({"context_window": 256000}),
        encoding="utf-8",
    )

    cfg = await (await Settings.create(str(tmp_path))).build_config()
    assert cfg.model.context_window == 256000


async def test_parallel_subagents_settings_round_trip(tmp_path):
    settings = Settings(str(tmp_path))

    assert settings.get_parallel_subagents() == ParallelSubagentsConfig()

    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))

    loaded = Settings(str(tmp_path))
    assert loaded.get_parallel_subagents() == ParallelSubagentsConfig(
        enabled=True,
        max_concurrent=3,
    )
    assert (await loaded.build_config()).parallel_subagents == ParallelSubagentsConfig(
        enabled=True,
        max_concurrent=3,
    )


def test_update_check_settings_round_trip(tmp_path):
    settings = Settings(str(tmp_path))

    assert settings.get_update_check_enabled() is True
    assert settings.update_check_due(now=1000) is True
    assert settings.get_update_check_last_checked_at() is None
    assert settings.get_update_check_latest_version() is None

    settings.mark_update_check("9.0.0", now=1000)

    loaded = Settings(str(tmp_path))
    assert loaded.get_update_check_enabled() is True
    assert loaded.get_update_check_last_checked_at() == 1000
    assert loaded.get_update_check_latest_version() == "9.0.0"
    assert loaded.update_check_due(now=1000 + 60) is False
    assert loaded.update_check_due(now=1000 + 24 * 60 * 60) is True

    loaded.set_update_check_enabled(False)

    disabled = Settings(str(tmp_path))
    assert disabled.get_update_check_enabled() is False
    assert disabled.update_check_due(now=1000 + 48 * 60 * 60) is False


async def test_user_profile_round_trips_and_builds_config(tmp_path):
    settings = Settings(str(tmp_path))

    assert settings.get_user_profile() == UserProfile()

    settings.set_user_language("zh-CN")
    settings.set_user_tone("direct")

    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["userProfile"] == {"language": "zh-CN", "tone": "direct"}

    reloaded = await Settings.create(str(tmp_path))
    assert reloaded.get_user_profile() == UserProfile(language="zh-CN", tone="direct")
    cfg = await reloaded.build_config()
    assert cfg.user_profile == UserProfile(language="zh-CN", tone="direct")

    reloaded.set_user_language("auto")
    assert reloaded.get_user_profile() == UserProfile(tone="direct")
    saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert saved["userProfile"] == {"tone": "direct"}


def test_user_profile_save_cleans_legacy_workspace_keys(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".voidx").mkdir()
    (workspace / ".voidx" / "settings.json").write_text(
        json.dumps({
            "user_language": "en",
            "user_tone": "formal",
        }),
        encoding="utf-8",
    )

    settings = Settings(str(workspace))
    path = settings.set_user_language("zh-CN")

    assert path == tmp_path / ".voidx" / "settings.json"
    global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
    assert global_saved["userProfile"] == {"language": "zh-CN", "tone": "formal"}
    assert workspace_saved == {}


async def test_permission_mode_determines_derived_sandbox_mode(tmp_path):
    settings = Settings(str(tmp_path))

    settings.set_permission_mode(PermissionMode.PROJECT_TRUSTED)
    cfg = await (await Settings.create(str(tmp_path))).build_config()

    assert cfg.permission_mode == PermissionMode.PROJECT_TRUSTED
    assert cfg.permission_mode.sandbox_mode == "workspace-write"


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

    assert state == {"version": 2, "enabled": ["docs", "legacy"], "disabled": [], "auto": []}
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


async def test_build_config_uses_pre_resolved_profile_once(monkeypatch, tmp_path):
    settings = Settings(str(tmp_path))
    profile = Profile(
        name="mimo/pre-resolved",
        api_key="sk-test",
        base_url="https://mimo.example/v1",
        protocol="openai",
    )

    async def fail_resolve_profile():
        raise AssertionError("resolve_profile should not be called")

    monkeypatch.setattr(settings, "resolve_profile", fail_resolve_profile)

    cfg = await settings.build_config(profile=profile)

    assert cfg.model.provider == "mimo"
    assert cfg.model.model == "pre-resolved"
    assert cfg.model.base_url == "https://mimo.example/v1"
    assert cfg.model.protocol == "openai"


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


async def test_current_profile_is_copied_from_global_for_new_workspace(tmp_path):
    global_profile = Profile(name=f"deepseek/{tmp_path.name}-global", api_key="sk-global")
    global_settings = Settings(str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    try:
        await global_settings.save_profile(global_profile, scope="global")

        settings = await Settings.create(str(workspace))

        workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert workspace_saved["current_profile"] == global_profile.name
        assert global_saved["current_profile"] == global_profile.name
        assert (await settings.resolve_profile()).name == global_profile.name
    finally:
        await delete_model_profile_async(global_profile.name)


async def test_plain_settings_constructor_does_not_write_current_profile_copy(tmp_path):
    global_profile = Profile(name=f"deepseek/{tmp_path.name}-global", api_key="sk-global")
    global_settings = Settings(str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    try:
        await global_settings.save_profile(global_profile, scope="global")

        Settings(str(workspace))

        assert not (workspace / ".voidx" / "settings.json").exists()
    finally:
        await delete_model_profile_async(global_profile.name)


async def test_local_model_switch_does_not_update_global_current_profile(tmp_path):
    global_profile = Profile(name=f"deepseek/{tmp_path.name}-global", api_key="sk-global")
    local_profile = Profile(name=f"mimo/{tmp_path.name}-local", api_key="sk-local")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_settings = Settings(str(tmp_path))

    try:
        await global_settings.save_profile(global_profile, scope="global")
        settings = await Settings.create(str(workspace))
        await settings.save_profile(local_profile)

        workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert workspace_saved["current_profile"] == local_profile.name
        assert global_saved["current_profile"] == global_profile.name
    finally:
        await delete_model_profile_async(global_profile.name)
        await delete_model_profile_async(local_profile.name)


async def test_global_model_switch_updates_global_and_current_workspace(tmp_path):
    old_profile = Profile(name=f"deepseek/{tmp_path.name}-old", api_key="sk-old")
    new_profile = Profile(name=f"mimo/{tmp_path.name}-new", api_key="sk-new")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_settings = Settings(str(tmp_path))

    try:
        await global_settings.save_profile(old_profile, scope="global")
        settings = await Settings.create(str(workspace))
        await settings.save_profile(new_profile, scope="global")

        workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert workspace_saved["current_profile"] == new_profile.name
        assert global_saved["current_profile"] == new_profile.name
    finally:
        await delete_model_profile_async(old_profile.name)
        await delete_model_profile_async(new_profile.name)


async def test_delete_profile_fallback_writes_local_only(tmp_path):
    global_profile = Profile(name=f"deepseek/{tmp_path.name}-global", api_key="sk-global")
    local_profile = Profile(name=f"mimo/{tmp_path.name}-local", api_key="sk-local")
    fallback_profile = Profile(name=f"openai/{tmp_path.name}-fallback", api_key="sk-fallback")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_settings = Settings(str(tmp_path))

    try:
        await global_settings.save_profile(global_profile, scope="global")
        settings = await Settings.create(str(workspace))
        await settings.save_profile(fallback_profile)
        await settings.save_profile(local_profile)

        await settings.delete_profile(local_profile.name)

        workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert workspace_saved["current_profile"] == fallback_profile.name
        assert global_saved["current_profile"] == global_profile.name
    finally:
        await delete_model_profile_async(global_profile.name)
        await delete_model_profile_async(local_profile.name)
        await delete_model_profile_async(fallback_profile.name)


@pytest.mark.asyncio
async def test_settings_ignores_legacy_agent_max_steps(tmp_path):
    settings = Settings(str(tmp_path))
    settings._data["agent_max_steps"] = {
        "voidx": 500,
        "review": 500,
        "recursion_limit": 1000,
    }

    config = await settings.build_config()

    assert not hasattr(config, "agent_max_steps")
