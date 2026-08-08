from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


MAIN_PATH = Path(__file__).parents[2] / "voidx" / "main.py"


def test_main_does_not_import_graph_implementation() -> None:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))

    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not any(module.startswith("voidx.agent.infrastructure.langgraph.runtime") for module in imported_modules)
    assert "LangGraphExecution" not in imported_names


@pytest.mark.asyncio
async def test_run_chat_builds_agent_through_composition(monkeypatch, tmp_path) -> None:
    from voidx.config import Config
    from voidx.llm.domain.model import ModelConfig
    from voidx.main import _run_chat

    captured = SimpleNamespace(build_kwargs=None, run_kwargs=None)

    class FakeSettings:
        @classmethod
        async def create(cls, workspace: str, **kwargs):
            return cls()

        async def resolve_profile(self):
            return None

        async def build_config(self, *, profile=None):
            return Config(model=ModelConfig(provider="openai", model="gpt-test"))

        async def resolve_api_key(self, provider: str):
            return "resolved-key"

    class FakeAgentApp:
        async def run(self, **kwargs) -> None:
            captured.run_kwargs = kwargs

    def fake_build_agent_app(config, api_key, *, session=None, settings=None):
        captured.build_kwargs = {
            "config": config,
            "api_key": api_key,
            "session": session,
            "settings": settings,
        }
        return FakeAgentApp()

    async def fake_select_start_session(**kwargs):
        return None

    monkeypatch.setattr("voidx.config.Settings", FakeSettings)
    monkeypatch.setattr("voidx.bootstrap.agent.build_agent_app", fake_build_agent_app)
    monkeypatch.setattr("voidx.main._select_start_session", fake_select_start_session)

    await _run_chat(workspace=str(tmp_path), web=True, web_host="0.0.0.0", web_port=8123)

    assert captured.build_kwargs["api_key"] == "resolved-key"
    assert captured.build_kwargs["config"].workspace == str(tmp_path.resolve())
    assert isinstance(captured.build_kwargs["settings"], FakeSettings)
    assert captured.run_kwargs["web"] is True
    assert captured.run_kwargs["web_host"] == "0.0.0.0"
    assert captured.run_kwargs["web_port"] == 8123
    assert captured.run_kwargs["web_token"]
