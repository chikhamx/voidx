from pathlib import Path

import pytest

from voidx.agent.application.instruction import InstructionService
from voidx.config import Settings


@pytest.mark.asyncio
async def test_instruction_read_file_uses_mtime_ns_and_size_cache(tmp_path, monkeypatch):
    path = tmp_path / "AGENTS.md"
    path.write_text("one", encoding="utf-8")
    service = InstructionService(str(tmp_path))

    original_read_text = Path.read_text
    calls = 0

    def counting_read_text(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    assert await service._read_file(str(path)) == "one"
    assert await service._read_file(str(path)) == "one"
    assert calls == 1

    path.write_text("two!", encoding="utf-8")

    assert await service._read_file(str(path)) == "two!"
    assert calls == 2


@pytest.mark.asyncio
async def test_instruction_service_reuses_injected_skill_summary_provider(tmp_path):
    calls = []

    def summaries():
        calls.append(True)
        return ["- verify [auto]: Verify changes"]

    service = InstructionService(
        str(tmp_path),
        skill_summaries_provider=summaries,
    )

    first = await service.available_skills_section()
    second = await service.available_skills_section()

    assert first == second
    assert calls == [True, True]


@pytest.mark.asyncio
async def test_system_paths_uses_first_instruction_file_at_first_matching_level(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    claude = workspace / "CLAUDE.md"
    agents.write_text("agents", encoding="utf-8")
    claude.write_text("claude", encoding="utf-8")

    service = InstructionService(str(workspace))

    assert await service.system_paths() == [str(agents.resolve())]


@pytest.mark.asyncio
async def test_resolve_injects_instruction_files_when_debug(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    package = tmp_path / "pkg"
    src = package / "src"
    src.mkdir(parents=True)
    instruction = package / "AGENTS.md"
    target = src / "app.py"
    instruction.write_text("Follow package rules.", encoding="utf-8")
    target.write_text("print('hi')\n", encoding="utf-8")
    service = InstructionService(str(tmp_path))
    service.set_debug(True)

    resolved = await service.resolve(str(target), "msg-1")

    assert resolved == [f"Instructions from: {instruction.resolve()}\nFollow package rules."]


@pytest.mark.asyncio
async def test_instruction_read_file_logs_exception_and_clears_cache(tmp_path, monkeypatch):
    import voidx.agent.application.instruction as instruction_module

    path = tmp_path / "AGENTS.md"
    path.write_text("one", encoding="utf-8")
    service = InstructionService(str(tmp_path))
    resolved = str(path.resolve())
    service._file_cache[resolved] = instruction_module._FileContentCacheEntry(
        mtime_ns=1,
        size=1,
        content="cached",
    )
    events = []

    async def failing_to_thread(func, *args, **kwargs):
        raise OSError("cannot stat")

    def fake_log_tool_event(event, *, tool_name="", message="", **kwargs):
        events.append((event, tool_name, message))

    monkeypatch.setattr(instruction_module.asyncio, "to_thread", failing_to_thread)
    monkeypatch.setattr(instruction_module, "log_tool_event", fake_log_tool_event)

    assert await service._read_file(str(path)) == ""
    assert resolved not in service._file_cache
    assert events == [("instruction_read_file", "instruction", "cannot stat")]


@pytest.mark.asyncio
async def test_resolve_retries_unclaimed_instruction_after_transient_read_failure(tmp_path, monkeypatch):
    package = tmp_path / "package"
    source = package / "src"
    source.mkdir(parents=True)
    instruction = package / "AGENTS.md"
    target = source / "app.py"
    instruction.write_text("Follow package rules.", encoding="utf-8")
    target.write_text("print('hi')\n", encoding="utf-8")
    service = InstructionService(str(tmp_path))
    reads = iter(["", "Follow package rules."])

    async def transient_read(_path):
        return next(reads)

    monkeypatch.setattr(service, "_read_file", transient_read)

    assert await service.resolve(str(target), "msg-1") == []
    assert await service.resolve(str(target), "msg-1") == [
        f"Instructions from: {instruction.resolve()}\nFollow package rules."
    ]


@pytest.mark.asyncio
async def test_execution_instruction_skill_summaries_follow_replaced_api(tmp_path):
    from types import SimpleNamespace

    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    execution = LangGraphExecution.__new__(LangGraphExecution)
    execution._workspace = str(tmp_path)
    execution.skills_api_provider = None
    execution.skills_api = SimpleNamespace(
        service=SimpleNamespace(available_skill_summaries=lambda: ["- old"])
    )
    execution._instruction = InstructionService(
        str(tmp_path),
        skill_summaries_provider=execution._available_skill_summaries,
    )

    execution.skills_api = SimpleNamespace(
        service=SimpleNamespace(available_skill_summaries=lambda: ["- new"])
    )

    assert "- new" in await execution._instruction.available_skills_section()
    assert "- old" not in await execution._instruction.available_skills_section()
