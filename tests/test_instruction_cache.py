from pathlib import Path

import pytest

from voidx.llm.instruction import InstructionService


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
