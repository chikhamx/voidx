from __future__ import annotations

import os

import pytest

from voidx.agent.application.automation.loop.prompt_materialize import (
    PromptMaterializeError,
    materialize_loop_prompt,
)


def test_prompt_without_references_is_returned_unchanged(tmp_path) -> None:
    prompt = "check build every hour"

    assert materialize_loop_prompt(prompt, str(tmp_path)) == prompt


def test_markdown_reference_is_inlined_as_snapshot(tmp_path) -> None:
    doc = tmp_path / "docs" / "tasks.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Tasks\n\n- fix the flaky test\n", encoding="utf-8")

    result = materialize_loop_prompt("process @docs/tasks.md carefully", str(tmp_path))

    assert "process @docs/tasks.md carefully" in result
    assert "snapshot" in result
    assert "docs/tasks.md" in result
    assert "- fix the flaky test" in result


def test_quoted_reference_is_supported(tmp_path) -> None:
    doc = tmp_path / "my docs" / "spec.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("spec body", encoding="utf-8")

    result = materialize_loop_prompt('read @"my docs/spec.md"', str(tmp_path))

    assert "spec body" in result


def test_script_reference_is_not_inlined_but_gets_execution_guidance(tmp_path) -> None:
    script = tmp_path / "bin" / "fetch_tasks.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho secret-task-body\n", encoding="utf-8")

    result = materialize_loop_prompt("run loop for @bin/fetch_tasks.sh", str(tmp_path))

    assert "secret-task-body" not in result
    assert "bin/fetch_tasks.sh" in result
    assert "bash" in result
    assert "every loop iteration" in result


def test_executable_bit_marks_script_without_known_suffix(tmp_path) -> None:
    script = tmp_path / "tasks"
    script.write_text("echo hi\n", encoding="utf-8")
    os.chmod(script, 0o755)

    result = materialize_loop_prompt("handle @tasks", str(tmp_path))

    assert "echo hi" not in result
    assert "every loop iteration" in result


def test_shebang_marks_script_without_known_suffix(tmp_path) -> None:
    script = tmp_path / "runner"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")

    result = materialize_loop_prompt("handle @runner", str(tmp_path))

    assert "print('hi')" not in result
    assert "every loop iteration" in result


def test_mixed_references_snapshot_docs_and_guide_scripts(tmp_path) -> None:
    (tmp_path / "spec.md").write_text("doc-body", encoding="utf-8")
    (tmp_path / "fetch.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    result = materialize_loop_prompt("use @spec.md and @fetch.sh", str(tmp_path))

    assert "doc-body" in result
    assert "every loop iteration" in result


def test_missing_reference_raises(tmp_path) -> None:
    with pytest.raises(PromptMaterializeError, match="not found"):
        materialize_loop_prompt("process @nope/missing.md", str(tmp_path))


def test_directory_reference_raises(tmp_path) -> None:
    (tmp_path / "somedir").mkdir()

    with pytest.raises(PromptMaterializeError, match="directory"):
        materialize_loop_prompt("process @somedir", str(tmp_path))


def test_image_attachment_reference_raises(tmp_path) -> None:
    with pytest.raises(PromptMaterializeError, match="Image"):
        materialize_loop_prompt("look at @:image:clipboard-1", str(tmp_path))


def test_binary_file_raises(tmp_path) -> None:
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"\xff\xfe\x00\x01binary")

    with pytest.raises(PromptMaterializeError, match="text"):
        materialize_loop_prompt("process @data.bin", str(tmp_path))


def test_absolute_path_reference(tmp_path) -> None:
    doc = tmp_path / "abs.md"
    doc.write_text("absolute body", encoding="utf-8")

    result = materialize_loop_prompt(f"read @{doc}", str(tmp_path))

    assert "absolute body" in result


def test_large_document_is_truncated(tmp_path) -> None:
    doc = tmp_path / "big.md"
    doc.write_text("x" * 300_000, encoding="utf-8")

    result = materialize_loop_prompt("read @big.md", str(tmp_path))

    assert "truncated" in result
    assert len(result) < 300_000 + 2000


def test_reference_inside_pasted_block_is_ignored(tmp_path) -> None:
    prompt = "do things\n<pasted>\nsome code with @decorator and @ghost.md\n</pasted>"

    assert materialize_loop_prompt(prompt, str(tmp_path)) == prompt


def test_tilde_reference_expands_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "tasks.md").write_text("home tasks", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    result = materialize_loop_prompt("read @~/tasks.md", str(tmp_path))

    assert "home tasks" in result


def test_bracket_image_token_raises(tmp_path) -> None:
    with pytest.raises(PromptMaterializeError, match="[Ii]mage"):
        materialize_loop_prompt("look at [image-clipboard-1]", str(tmp_path))


def test_truncation_at_multibyte_boundary_still_succeeds(tmp_path) -> None:
    doc = tmp_path / "multibyte.md"
    # 汉字 is 3 bytes in UTF-8; pad so the 200KB cut lands mid-character.
    body = "汉" * 66_667 + "x"  # 66_667*3 + 1 = 200_002 bytes
    doc.write_text(body, encoding="utf-8")

    result = materialize_loop_prompt("read @multibyte.md", str(tmp_path))

    assert "truncated" in result
    assert "汉" in result
