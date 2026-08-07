import voidx.presentation.output.browse as browse_module


def test_browse_invalid_mouse_row():
    row = browse_module._mouse_row_from_sequence("0;12;not-a-row")
    assert row is None


def test_browse_invalid_mouse_sequence_decode():
    row = browse_module._mouse_row_from_sequence(b"\xff")
    assert row is None
