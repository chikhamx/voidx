from pathlib import Path

import pytest

from voidx.llm.instruction import InstructionService
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
async def test_instruction_service_reuses_skill_service_until_selection_changes(tmp_path):
    settings = Settings(str(tmp_path))
    service = InstructionService(str(tmp_path), settings=settings)

    first_registry = service._skill_registry
    first_service = service._skill_service_for_current_selection()
    await service.available_skills_section()
    second_service = service._skill_service_for_current_selection()

    settings.set_skill_enabled("verify", False)
    third_service = service._skill_service_for_current_selection()

    assert service._skill_registry is first_registry
    assert second_service is first_service
    assert third_service is not first_service
    assert service._skill_registry is first_registry


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
    import voidx.llm.instruction as instruction_module

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
