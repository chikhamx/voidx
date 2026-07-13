from __future__ import annotations

from pathlib import Path

import pytest

from voidx.agent.loop.prompt_source import PromptSource
from voidx.tools.base import ToolContext, ToolResult


class FakeBashTool:
    def __init__(self, output: str = "script output") -> None:
        self.output = output
        self.calls: list[tuple[dict, ToolContext]] = []

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append((args, ctx))
        return ToolResult(output=self.output)


@pytest.mark.asyncio
async def test_text_prompt_resolves_verbatim(tmp_path: Path) -> None:
    source = PromptSource.from_raw("check the build")

    assert source.kind == "text"
    assert await source.resolve(str(tmp_path)) == "check the build"


@pytest.mark.asyncio
async def test_file_reference_resolves_current_file_content(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("review this", encoding="utf-8")
    source = PromptSource.from_raw("@prompt.md")

    assert source.kind == "file"
    assert await source.resolve(str(tmp_path)) == "review this"

    prompt_file.write_text("review updated", encoding="utf-8")
    assert await source.resolve(str(tmp_path)) == "review updated"


@pytest.mark.asyncio
async def test_outside_workspace_reference_returns_loop_error(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-loop-prompt.txt"
    outside.write_text("secret", encoding="utf-8")
    source = PromptSource.from_raw(f"@{outside}")

    resolved = await source.resolve(str(tmp_path))

    assert resolved.startswith("[loop] prompt source error:")
    assert "outside workspace" in resolved
    assert "secret" not in resolved


@pytest.mark.asyncio
async def test_script_reference_uses_bash_tool_with_quoted_path(tmp_path: Path) -> None:
    script = tmp_path / "status script.sh"
    script.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    script.chmod(0o755)
    source = PromptSource.from_raw('@"status script.sh"')
    bash = FakeBashTool(output="deploy ok")
    ctx = ToolContext(workspace=str(tmp_path))

    resolved = await source.resolve(str(tmp_path), bash_tool=bash, ctx=ctx)

    assert resolved == "deploy ok"
    assert len(bash.calls) == 1
    command = bash.calls[0][0]["command"]
    assert "status script.sh" in command
    assert command != str(script)
    assert bash.calls[0][0]["timeout"] == 30
