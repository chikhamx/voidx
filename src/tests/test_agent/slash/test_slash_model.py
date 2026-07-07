import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from voidx.agent.slash.runtime import _select_from_list
from voidx.agent.task_state import GoalSpec, TaskState
from voidx.config import (
    CodeIde,
    ApprovalPolicy,
    ApprovalReviewer,
    Config,
    McpServerConfig,
    ModelConfig,
    ParallelSubagentsConfig,
    PermissionMode,
    SandboxMode,
    Settings,
    UserProfile,
)
from voidx.permission.service import PermissionService
from voidx.llm.catalog import STATIC_MODELS
from voidx.llm.usage import UsageStats
from voidx.memory.model_profiles import ModelProfileRow, save_model_profile_async
from voidx.memory.model_profiles import delete_model_profile_async
from voidx.ui.tools.clipboard_image import ClipboardImageResult


class FakeChoiceApp:
    def __init__(self, result: str | None = None, text_result: str | None = None) -> None:
        self.result = result
        self.text_result = text_result
        self.prompt = ""
        self.choices = []
        self.text_prompt = ""
        self.text_secret = False

    async def ask_choice(self, prompt, choices, details=None):
        self.prompt = prompt
        self.choices = choices
        return self.result

    async def ask_text(self, prompt, default="", secret=False):
        self.text_prompt = prompt
        self.text_secret = secret
        if self.text_result is not None:
            return self.text_result
        return self.result


def _capture_handler_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.agent.slash.handler.ui.print",
        lambda text="": output.append(str(text)),
    )
    monkeypatch.setattr(
        "voidx.agent.slash.handler.ui.error",
        lambda text="": output.append(f"ERROR: {text}"),
    )
    return output



async def test_select_from_list_uses_prompt_app_choice():
    app = FakeChoiceApp(result="1")

    selected = await _select_from_list(app, "Provider", ["anthropic", "mimo"])

    assert selected == 1
    assert app.prompt == "Provider"
    assert app.choices == [
        ("anthropic", "0", ""),
        ("mimo", "1", ""),
    ]


@pytest.mark.asyncio
async def test_model_list_reads_settings_profiles(tmp_path):
    profile_one = f"mimo/{tmp_path.name}-v2.5"
    profile_two = f"openai/{tmp_path.name}-gpt-4o"
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "default_profile": profile_one,
            "profiles": {
                profile_one: {"api_key": "sk-test-1"},
                profile_two: {"api_key": "sk-test-2"},
            },
        }),
        encoding="utf-8",
    )
    try:
        settings = await Settings.create(str(tmp_path))
        graph = SimpleNamespace(
            config=await settings.build_config(),
            _settings=settings,
            model=object(),
            _app=None,
        )

        await SlashHandler(graph)._model_list()
    finally:
        await delete_model_profile_async(profile_one)
        await delete_model_profile_async(profile_two)


@pytest.mark.asyncio
async def test_model_test_dispatch_strips_command_prefix():
    graph = SimpleNamespace()
    handler = SlashHandler(graph)
    targets: list[str] = []

    async def fake_model_test(target: str) -> None:
        targets.append(target)

    handler._model_test = fake_model_test

    assert await handler.dispatch("/model test") is True
    assert await handler.dispatch("/model test mimo/mimo-v2.5") is True

    assert targets == ["", "mimo/mimo-v2.5"]


@pytest.mark.asyncio
async def test_slash_dispatch_rejects_unknown_prefix_command():
    assert await SlashHandler(SimpleNamespace()).dispatch("/debugx") is False


@pytest.mark.asyncio
async def test_model_test_creates_model_with_config_defaults(tmp_path, monkeypatch):
    profile_name = f"openai/{tmp_path.name}-gpt-5.4-mini"
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "default_profile": profile_name,
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
        graph = SimpleNamespace(
            config=await settings.build_config(),
            _settings=settings,
            _app=None,
        )
        captured = {}

        def fake_create_chat_model(api_key, cfg):
            captured["reasoning_effort"] = cfg.reasoning_effort
            return object()

        monkeypatch.setattr("voidx.llm.provider.create_chat_model", fake_create_chat_model)
        handler = SlashHandler(graph)

        async def fake_test_connection(_model):
            return True, ""

        handler._test_connection = fake_test_connection

        await handler._model_test(profile_name)

        assert captured["reasoning_effort"] == "xhigh"
    finally:
        await delete_model_profile_async(profile_name)


@pytest.mark.asyncio
async def test_model_prompt_uses_prompt_app_text_input():
    app = FakeChoiceApp(result="sk-test")
    graph = SimpleNamespace(_app=app)

    result = await SlashHandler(graph)._prompt("API key", secret=True)

    assert result == "sk-test"
    assert app.text_prompt == "API key"
    assert app.text_secret is True


@pytest.mark.asyncio
async def test_model_switch_defaults_to_local_scope(tmp_path, monkeypatch):
    profile_name = f"deepseek/{tmp_path.name}-local"
    settings = await Settings.create(str(tmp_path))
    await save_model_profile_async(ModelProfileRow(
        name=profile_name,
        provider="deepseek",
        model=f"{tmp_path.name}-local",
        api_key="sk-local",
    ))
    graph = SimpleNamespace(
        config=Config(model=ModelConfig(provider="deepseek", model="old")),
        api_key="sk-old",
        model=object(),
        _settings=settings,
        _session=None,
        _app=None,
        _usage_stats=None,
    )
    monkeypatch.setattr("voidx.llm.provider.create_chat_model", lambda *_args: object())

    try:
        await SlashHandler(graph).dispatch(f"/model switch {profile_name}")

        saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert saved["current_profile"] == profile_name
    finally:
        await delete_model_profile_async(profile_name)


@pytest.mark.asyncio
async def test_model_switch_global_scope_updates_global_and_local(tmp_path, monkeypatch):
    profile_name = f"deepseek/{tmp_path.name}-global"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = await Settings.create(str(workspace))
    await save_model_profile_async(ModelProfileRow(
        name=profile_name,
        provider="deepseek",
        model=f"{tmp_path.name}-global",
        api_key="sk-global",
    ))
    graph = SimpleNamespace(
        config=Config(model=ModelConfig(provider="deepseek", model="old")),
        api_key="sk-old",
        model=object(),
        _settings=settings,
        _session=None,
        _app=None,
        _usage_stats=None,
    )
    monkeypatch.setattr("voidx.llm.provider.create_chat_model", lambda *_args: object())

    try:
        await SlashHandler(graph).dispatch(f"/model switch {profile_name} --global")

        workspace_saved = json.loads((workspace / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        global_saved = json.loads((tmp_path / ".voidx" / "settings.json").read_text(encoding="utf-8"))
        assert workspace_saved["current_profile"] == profile_name
        assert global_saved["current_profile"] == profile_name
    finally:
        await delete_model_profile_async(profile_name)


@pytest.mark.asyncio

def test_model_provider_list_matches_catalog():
    from voidx.agent.slash.runtime import PROVIDERS

    assert set(STATIC_MODELS).issubset(PROVIDERS)


def test_slash_handler_uses_runtime_ui_singleton():
    from voidx.agent.slash.handler import ui as slash_ui
    from voidx.runtime.ui import ui as runtime_ui

    assert slash_ui is runtime_ui


def test_model_status_sync_updates_prompt_footer_state():
    status = SimpleNamespace(
        provider="old",
        model="old",
        reasoning_effort="medium",
        context_limit=0,
    )
    graph = SimpleNamespace(
        config=SimpleNamespace(
            model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high")
        ),
        _usage_stats=UsageStats(),
        _app=SimpleNamespace(status=status),
    )

    SlashHandler(graph)._sync_context_limit()

    assert status.provider == "mimo"
    assert status.model == "mimo-v2.5"
    assert status.reasoning_effort == "high"
    assert status.context_limit == 1_000_000


def test_model_status_sync_uses_context_window_override():
    """context_window 设置后，_sync_context_limit 用 override 值而非 provider 查表。"""
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        reasoning_effort="high",
        context_limit=0,
    )
    graph = SimpleNamespace(
        config=SimpleNamespace(
            model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high", context_window=256_000)
        ),
        _usage_stats=UsageStats(),
        _app=SimpleNamespace(status=status),
    )

    SlashHandler(graph)._sync_context_limit()

    assert status.context_limit == 256_000


@pytest.mark.asyncio
async def test_model_dispatch_without_args_opens_switch_picker():
    graph = SimpleNamespace()
    handler = SlashHandler(graph)
    targets: list[str] = []

    async def fake_model_switch(target: str) -> None:
        targets.append(target)

    handler._model_switch = fake_model_switch

    assert await handler.dispatch("/model") is True

    assert targets == [""]


@pytest.mark.asyncio
async def test_model_new_and_del_dispatch_to_matching_methods():
    graph = SimpleNamespace()
    handler = SlashHandler(graph)
    calls: list[tuple[str, str]] = []

    async def fake_model_new() -> None:
        calls.append(("new", ""))

    async def fake_model_del(target: str) -> None:
        calls.append(("del", target))

    handler._model_new = fake_model_new
    handler._model_del = fake_model_del

    assert await handler.dispatch("/model new") is True
    assert await handler.dispatch("/model del mimo/mimo-v2.5") is True

    assert calls == [("new", ""), ("del", "mimo/mimo-v2.5")]


@pytest.mark.asyncio
async def test_model_ctx_direct_value_sets_and_persists(tmp_path):
    """/model ctx 256k 设置 context_window 并持久化到配置文件。"""
    settings = await Settings.create(str(tmp_path))
    status = SimpleNamespace(context_limit=0, provider="mimo", model="mimo-v2.5", reasoning_effort="high")
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=SimpleNamespace(status=status),
        _usage_stats=UsageStats(),
    )

    assert await SlashHandler(graph).dispatch("/model ctx 256k") is True

    assert graph.config.model.context_window == 256_000
    assert status.context_limit == 256_000
    reloaded = await Settings.create(str(tmp_path))
    assert reloaded._effective_data().get("context_window") == 256_000


@pytest.mark.asyncio
async def test_model_ctx_auto_removes_persisted_key(tmp_path):
    """/model ctx auto 移除持久化键，context_window 回到 None。"""
    settings = await Settings.create(str(tmp_path))
    settings._set_setting("context_window", 256000)
    status = SimpleNamespace(context_limit=0, provider="mimo", model="mimo-v2.5", reasoning_effort="high")
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=SimpleNamespace(status=status),
        _usage_stats=UsageStats(),
    )

    assert await SlashHandler(graph).dispatch("/model ctx auto") is True

    assert graph.config.model.context_window is None
    reloaded = await Settings.create(str(tmp_path))
    assert "context_window" not in reloaded._effective_data()


@pytest.mark.asyncio
async def test_model_ctx_invalid_value_errors(tmp_path):
    """/model ctx 999x 无效值时报错，不修改 context_window。"""
    settings = await Settings.create(str(tmp_path))
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=None,
        _usage_stats=UsageStats(),
    )
    original = graph.config.model.context_window

    assert await SlashHandler(graph).dispatch("/model ctx 999x") is True

    assert graph.config.model.context_window == original


@pytest.mark.asyncio
async def test_model_ctx_picker_selects_value(tmp_path):
    """/model ctx 无参数时弹出选项框，选择后设置并持久化。"""
    settings = await Settings.create(str(tmp_path))
    app = FakeChoiceApp(result="1")  # 选择第 1 项 = 256k
    status = SimpleNamespace(context_limit=0, provider="mimo", model="mimo-v2.5", reasoning_effort="high")
    app.status = status
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=app,
        _usage_stats=UsageStats(),
    )

    assert await SlashHandler(graph).dispatch("/model ctx") is True

    assert graph.config.model.context_window == 256_000
    assert status.context_limit == 256_000
    assert app.prompt == "Context window"


@pytest.mark.asyncio
async def test_model_ctx_picker_cancel_does_nothing(tmp_path):
    """/model ctx 选项框取消时不修改 context_window。"""
    settings = await Settings.create(str(tmp_path))
    app = FakeChoiceApp(result=None)  # 取消
    graph = SimpleNamespace(
        config=await settings.build_config(),
        _settings=settings,
        _app=app,
        _usage_stats=UsageStats(),
    )
    original = graph.config.model.context_window

    assert await SlashHandler(graph).dispatch("/model ctx") is True

    assert graph.config.model.context_window == original


@pytest.mark.asyncio
async def test_paste_dispatch_uses_core_clipboard_tool(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_paste_clipboard_image(workspace: str):
        calls.append(workspace)
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
        )

    monkeypatch.setattr(
        "voidx.agent.slash.handler.paste_clipboard_image",
        fake_paste_clipboard_image,
    )
    graph = SimpleNamespace(_workspace=str(tmp_path), _app=object())

    assert await SlashHandler(graph).dispatch("/paste") is True
    assert calls == [str(tmp_path)]


@pytest.mark.asyncio
async def test_usage_dispatch_reads_usage_stats():
    graph = SimpleNamespace(
        _usage_stats=UsageStats(
            context_tokens=100,
            context_limit=1_000,
            last_input_tokens=100,
            last_output_tokens=20,
            total_input_tokens=200,
            total_output_tokens=40,
            total_calls=2,
        )
    )

    assert await SlashHandler(graph).dispatch("/usage") is True


@pytest.mark.asyncio
async def test_permission_mode_dispatch_updates_service_and_settings(tmp_path):
    settings = Settings(str(tmp_path))
    permission = PermissionService()
    graph = SimpleNamespace(
        _permission=permission,
        _settings=settings,
        _app=None,
    )

    assert await SlashHandler(graph).dispatch("/permission-mode full-access") is True

    reloaded = await Settings.create(str(tmp_path))
    cfg = await reloaded.build_config()
    assert permission.permission_mode == "full-access"
    assert permission.sandbox_mode == "danger-full-access"
    assert permission.approval_policy == "never"
    assert cfg.permission_mode == PermissionMode.FULL_ACCESS
    assert cfg.sandbox_mode == SandboxMode.DANGER_FULL_ACCESS
    assert cfg.approval_policy == ApprovalPolicy.NEVER
    assert cfg.approval_reviewer == ApprovalReviewer.USER


@pytest.mark.asyncio
async def test_parallel_toggle_on_persists_without_live_config_update(tmp_path, monkeypatch):
    output = _capture_handler_output(monkeypatch)
    settings = Settings(str(tmp_path))
    graph = SimpleNamespace(
        config=Config(workspace=str(tmp_path)),
        _settings=settings,
    )

    assert await SlashHandler(graph).dispatch("/parallel on") is True

    assert graph.config.parallel_subagents.enabled is False
    assert Settings(str(tmp_path)).get_parallel_subagents() == ParallelSubagentsConfig(enabled=True)
    assert output == [
        "[dim]Saved parallel subagents on (max_concurrent=4). Run /clear or restart to apply.[/dim]"
    ]



@pytest.mark.asyncio
async def test_model_switch_profile_updates_session_db(tmp_path, monkeypatch):
    """/model switch <profile> 应同步更新 sessiondb 的 model_provider/model_name。"""
    profile_name = f"deepseek/{tmp_path.name}-switch-sync"
    settings = await Settings.create(str(tmp_path))
    await save_model_profile_async(ModelProfileRow(
        name=profile_name,
        provider="deepseek",
        model=f"{tmp_path.name}-switch-sync",
        api_key="sk-test",
    ))
    session = SimpleNamespace(id="sess-123")
    graph = SimpleNamespace(
        config=Config(model=ModelConfig(provider="deepseek", model="old")),
        api_key="sk-old",
        model=object(),
        _settings=settings,
        _session=session,
        _app=None,
        _usage_stats=None,
    )
    monkeypatch.setattr("voidx.llm.provider.create_chat_model", lambda *_a, **_k: object())

    captured: list[tuple[str, str, str]] = []

    async def fake_update_session_model(session_id, provider, model):
        captured.append((session_id, provider, model))

    monkeypatch.setattr("voidx.memory.service.update_session_model", fake_update_session_model)

    try:
        await SlashHandler(graph).dispatch(f"/model switch {profile_name}")
        assert captured == [("sess-123", "deepseek", f"{tmp_path.name}-switch-sync")]
    finally:
        await delete_model_profile_async(profile_name)


@pytest.mark.asyncio
async def test_switch_model_spec_calls_show_startup(tmp_path, monkeypatch):
    """/model <provider/model> 切换后应调用 _show_startup 展示新模型信息。"""
    settings = await Settings.create(str(tmp_path))
    await save_model_profile_async(ModelProfileRow(
        name="deepseek/deepseek-v4-pro",
        provider="deepseek",
        model="deepseek-v4-pro",
        api_key="sk-test",
    ))
    graph = SimpleNamespace(
        config=Config(model=ModelConfig(provider="anthropic", model="old")),
        api_key="sk-old",
        model=object(),
        _settings=settings,
        _session=None,
        _app=None,
        _usage_stats=None,
    )
    monkeypatch.setattr("voidx.llm.provider.create_chat_model", lambda *_a, **_k: object())

    startup_calls: list[dict] = []

    async def fake_show_startup(**kwargs):
        startup_calls.append(kwargs)

    handler = SlashHandler(graph)
    handler._show_startup = fake_show_startup

    try:
        await handler.dispatch("/model deepseek/deepseek-v4-pro")
        assert len(startup_calls) == 1
        assert startup_calls[0].get("prefer_direct") is True
    finally:
        await delete_model_profile_async("deepseek/deepseek-v4-pro")
