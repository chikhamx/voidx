"""Contract tests for semantic find/search tools."""

import json
import os
from pathlib import Path

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


async def execute(tool, args, workspace):
    return await ToolRegistry().execute_tool(tool, args, ToolContext(workspace=str(workspace)))


@pytest.mark.asyncio
async def test_find_filters_and_sorts_files(tmp_path):
    (tmp_path / "z.py").touch()
    (tmp_path / "Search.py").touch()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "search_test.py").touch()
    result = await execute("find", {"query": "search", "extensions": ["py"], "case": "insensitive"}, tmp_path)
    assert json.loads(result.output)["files"] == [
        {"path": "Search.py", "name": "Search.py"},
        {"path": "sub/search_test.py", "name": "search_test.py"},
    ]


@pytest.mark.asyncio
async def test_find_auto_case_respects_uppercase_query(tmp_path):
    (tmp_path / "AGENTS.md").touch()
    data = json.loads((await execute("find", {"query": "AGENTS", "extensions": ["md"]}, tmp_path)).output)
    assert data["files"] == [{"path": "AGENTS.md", "name": "AGENTS.md"}]


@pytest.mark.asyncio
async def test_find_requires_filter(tmp_path):
    result = await execute("find", {}, tmp_path)
    assert result.metadata["error"] is True


@pytest.mark.asyncio
async def test_find_rejects_path_separator_query(tmp_path):
    result = await execute("find", {"query": "src/foo"}, tmp_path)
    assert result.metadata["error"] is True


@pytest.mark.asyncio
async def test_find_excludes_hidden_symlinks_and_gitignored(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "visible.py").touch()
    (tmp_path / "ignored.py").touch()
    (tmp_path / ".hidden.py").touch()
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    (hidden / "nested.py").touch()
    if os.name != "nt":
        (tmp_path / "link.py").symlink_to(tmp_path / "visible.py")
    data = json.loads((await execute("find", {"extensions": ["py"]}, tmp_path)).output)
    assert data["files"] == [
        {"path": ".hidden.py", "name": ".hidden.py"},
        {"path": "visible.py", "name": "visible.py"},
    ]




@pytest.mark.asyncio
async def test_find_prunes_skip_directories(tmp_path):
    for directory in (".git", "node_modules", "__pycache__"):
        nested = tmp_path / directory
        nested.mkdir()
        (nested / "hidden.py").touch()
    data = json.loads((await execute("find", {"extensions": ["py"]}, tmp_path)).output)
    assert data["files"] == []
@pytest.mark.asyncio
async def test_search_literal_is_default(tmp_path):
    (tmp_path / "a.py").write_text("x[0] = 1\nregex x0\n")
    data = json.loads((await execute("search", {"query": "x[0]", "path": "a.py"}, tmp_path)).output)
    assert data["matches"][0]["hits"][0]["text"] == "x[0] = 1"
    assert data["matches"][0]["hits"][0]["column"] == 1


@pytest.mark.asyncio
async def test_search_regex_and_word_modes(tmp_path):
    (tmp_path / "a.txt").write_text("cat concatenate\ncat\n")
    regex = json.loads((await execute("search", {"query": r"cat\s+", "match": "regex", "path": "a.txt"}, tmp_path)).output)
    word = json.loads((await execute("search", {"query": "cat", "match": "word", "path": "a.txt"}, tmp_path)).output)
    assert len(regex["matches"][0]["hits"]) == 1
    assert len(word["matches"][0]["hits"]) == 2


@pytest.mark.asyncio
async def test_search_context_and_metadata_schema(tmp_path):
    (tmp_path / "a.txt").write_text("before\nneedle\nafter\n")
    result = await execute("search", {"query": "needle", "context": 1}, tmp_path)
    data = json.loads(result.output)
    hit = data["matches"][0]["hits"][0]
    assert hit["before"] == [{"line": 1, "text": "before"}]
    assert hit["after"] == [{"line": 3, "text": "after"}]
    assert set(result.metadata["match_details"][0]) >= {"path", "line", "column", "text", "before", "after"}


@pytest.mark.asyncio
async def test_search_skips_binary_and_gitignored_explicit_files(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("needle\n")
    (tmp_path / "binary.txt").write_bytes(b"needle\x00value")
    for name in ("ignored.txt", "binary.txt"):
        data = json.loads((await execute("search", {"query": "needle", "path": name}, tmp_path)).output)
        assert data["matches"] == []


@pytest.mark.asyncio
async def test_search_invalid_regex_is_structured_error(tmp_path):
    result = await execute("search", {"query": "[", "match": "regex"}, tmp_path)
    assert result.metadata["error"] is True


@pytest.mark.asyncio
async def test_search_truncates_hits(tmp_path):
    (tmp_path / "a.txt").write_text("needle\nneedle\n")
    data = json.loads((await execute("search", {"query": "needle", "max_results": 1}, tmp_path)).output)
    assert data["truncated"] is True
    assert len(data["matches"][0]["hits"]) == 1


@pytest.mark.asyncio
async def test_find_keeps_top_level_hidden_files(tmp_path):
    (tmp_path / ".env.py").touch()
    data = json.loads((await execute("find", {"extensions": ["py"]}, tmp_path)).output)
    assert data["files"] == [{"path": ".env.py", "name": ".env.py"}]



@pytest.mark.asyncio
async def test_find_accepts_max_results_500(tmp_path):
    for index in range(10):
        (tmp_path / f"test_{index:03d}.py").touch()
    result = await execute(
        "find",
        {"query": "test_", "extensions": ["py"], "max_results": 500},
        tmp_path,
    )
    assert result.metadata.get("error") is not True
    data = json.loads(result.output)
    assert len(data["files"]) == 10


@pytest.mark.asyncio
async def test_find_output_stays_within_char_budget_and_persists_overflow(tmp_path):
    for index in range(200):
        (tmp_path / f"test_{index:03d}_long_name_for_budget_check.py").touch()
    result = await execute(
        "find",
        {"query": "test_", "extensions": ["py"], "max_results": 500},
        tmp_path,
    )
    data = json.loads(result.output)
    assert len(result.output) <= 4000
    assert data["truncated"] is True
    assert 0 < len(data["files"]) < 200
    overflow_path = data.get("overflow_path")
    assert overflow_path
    overflow = json.loads(Path(overflow_path).read_text(encoding="utf-8"))
    assert len(overflow["files"]) == 200
    assert overflow["truncated"] is False


@pytest.mark.asyncio
async def test_search_output_stays_within_char_budget_and_persists_overflow(tmp_path):
    for index in range(80):
        (tmp_path / f"file_{index:03d}.txt").write_text(
            f"needle match with long surrounding text for budget pressure {index:03d}\n"
        )
    result = await execute(
        "search",
        {"query": "needle", "extensions": ["txt"], "max_results": 500},
        tmp_path,
    )
    data = json.loads(result.output)
    assert len(result.output) <= 4000
    assert data["truncated"] is True
    total_hits = sum(len(item["hits"]) for item in data["matches"])
    assert 0 < total_hits < 80
    overflow_path = data.get("overflow_path")
    assert overflow_path
    overflow = json.loads(Path(overflow_path).read_text(encoding="utf-8"))
    overflow_hits = sum(len(item["hits"]) for item in overflow["matches"])
    assert overflow_hits == 80




@pytest.mark.asyncio
async def test_search_marks_matching_lines_as_read(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("keep\nREMOVE_ME\n")
    ctx = ToolContext(workspace=str(tmp_path))
    registry = ToolRegistry()
    await registry.execute_tool("search", {"query": "REMOVE_ME", "path": "code.py"}, ctx)
    result = await registry.execute_tool(
        "replace",
        {"file_path": "code.py", "bounds": [{"line_no": 2, "anchor": "REMOVE_ME"}], "new_string": ""},
        ctx,
    )
    assert result.metadata.get("error") is not True
