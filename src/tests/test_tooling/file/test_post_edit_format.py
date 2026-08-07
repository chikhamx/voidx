import pytest

from voidx.tooling.domain.diff import make_structured_diff
from voidx.lsp.domain.errors import LspFormattingUnsupported, LspTimeoutError
from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.adapters.lsp_post_edit import LspPostEditFormatter, format_after_edit, format_range_from_diff


def _range(old: str, new: str):
    return format_range_from_diff(new, make_structured_diff("sample.py", old, new))


def test_format_range_uses_changed_lines_in_new_document():
    range_ = _range("one\ntwo\nthree\n", "one\nTWO\nthree\n")

    assert range_.start.line == 1
    assert range_.start.character == 0
    assert range_.end.line == 2
    assert range_.end.character == 0


def test_format_range_envelopes_multiple_hunks():
    range_ = _range("a\nb\nc\nd\ne\nf\ng\n", "A\nb\nc\nd\ne\nf\nG\n")

    assert range_.start.line == 0
    assert range_.end.line == 7


def test_format_range_anchors_pure_delete_at_surviving_line():
    range_ = _range("one\ntwo\nthree\n", "one\nthree\n")

    assert range_.start.line == 1
    assert range_.end.line == 2


def test_format_range_anchors_eof_delete_at_previous_line():
    range_ = _range("one\ntwo\n", "one\n")

    assert range_.start.line == 0
    assert range_.end.line == 1


def test_format_range_uses_utf16_end_for_last_line_without_newline():
    range_ = _range("old", "a😀b")

    assert range_.start.line == 0
    assert range_.end.line == 0
    assert range_.end.character == 4


def test_format_range_skips_empty_result():
    assert _range("one\n", "") is None


@pytest.mark.asyncio
async def test_format_after_edit_returns_unavailable_without_manager(tmp_path):
    result = await format_after_edit(
        None,
        tmp_path / "sample.py",
        display_path="sample.py",
        edited_text="one\n",
        format_range=_range("", "one\n"),
    )

    assert result.status == "unavailable"
    assert result.final_text == "one\n"


@pytest.mark.asyncio
async def test_format_after_edit_degrades_unsupported_and_timeout(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("one\n", encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            if file_path == "unsupported.py":
                raise LspFormattingUnsupported("unsupported")
            raise LspTimeoutError("timeout")

    ctx = LspPostEditFormatter(Manager())
    range_ = _range("", "one\n")

    unsupported = await format_after_edit(
        ctx, target, display_path="unsupported.py", edited_text="one\n", format_range=range_
    )
    timed_out = await format_after_edit(
        ctx, target, display_path="timeout.py", edited_text="one\n", format_range=range_
    )

    assert unsupported.status == "unsupported"
    assert timed_out.status == "failed"
    assert unsupported.final_text == timed_out.final_text == "one\n"


@pytest.mark.asyncio
async def test_format_after_edit_returns_actual_disk_text_on_commit_conflict(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    target.write_text(edited_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            target.write_text("external = True\n", encoding="utf-8")
            return True, edited_text, "print(1)\n"

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "failed"
    assert result.final_text == "external = True\n"
    assert target.read_text(encoding="utf-8") == result.final_text


@pytest.mark.asyncio
async def test_format_after_edit_reports_unknown_final_text_when_file_disappears(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    target.write_text(edited_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            target.unlink()
            return True, edited_text, "print(1)\n"

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "failed"
    assert result.final_text is None
    assert "final file state unavailable" in result.error


@pytest.mark.asyncio
async def test_format_after_edit_returns_actual_disk_text_when_lsp_source_changed(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    actual_text = "external = True\n"
    target.write_text(actual_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            return True, actual_text, "external=True\n"

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "failed"
    assert result.final_text == actual_text
    assert target.read_text(encoding="utf-8") == result.final_text


@pytest.mark.asyncio
async def test_format_after_edit_reads_actual_disk_text_when_request_fails(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    target.write_text(edited_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            target.write_text("external = True\n", encoding="utf-8")
            raise LspTimeoutError("timeout")

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "failed"
    assert result.final_text == "external = True\n"


@pytest.mark.asyncio
async def test_format_after_edit_reports_unknown_state_when_request_deletes_file(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    target.write_text(edited_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            target.unlink()
            raise LspTimeoutError("timeout")

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "failed"
    assert result.final_text is None
    assert "final file state unavailable" in result.error


@pytest.mark.asyncio
async def test_format_after_edit_reads_actual_disk_text_when_request_is_unsupported(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    target.write_text(edited_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            target.write_text("external = True\n", encoding="utf-8")
            raise LspFormattingUnsupported("unsupported")

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "unsupported"
    assert result.final_text == "external = True\n"


@pytest.mark.asyncio
async def test_format_after_edit_reads_actual_disk_text_when_request_is_unchanged(tmp_path):
    target = tmp_path / "sample.py"
    edited_text = "print( 1 )\n"
    target.write_text(edited_text, encoding="utf-8")

    class Manager:
        async def format_range(self, file_path, range_):
            target.write_text("external = True\n", encoding="utf-8")
            return False, edited_text, edited_text

    result = await format_after_edit(
        LspPostEditFormatter(Manager()),
        target,
        display_path="sample.py",
        edited_text=edited_text,
        format_range=_range("", edited_text),
    )

    assert result.status == "unchanged"
    assert result.final_text == "external = True\n"
