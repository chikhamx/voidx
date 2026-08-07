from voidx.presentation.tools.clipboard_text import read_clipboard_text


def test_clipboard_text_reads_non_empty_text():
    result = read_clipboard_text(capture_clipboard_text=lambda: ("ok", "hello"))

    assert result.ok
    assert result.text == "hello"


def test_clipboard_text_empty_text_returns_no_text():
    result = read_clipboard_text(capture_clipboard_text=lambda: ("ok", ""))

    assert result.status == "no_text"
    assert "does not contain text" in result.message


def test_clipboard_text_maps_errors():
    result = read_clipboard_text(capture_clipboard_text=lambda: ("error: denied", ""))

    assert result.status == "error"
    assert result.message == "denied"
