import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import voidx.ui.output.browse as browse_module


def test_browse_logs_invalid_mouse_row(caplog):
    with caplog.at_level(logging.DEBUG, logger="voidx.ui.output.browse"):
        row = browse_module._mouse_row_from_sequence("0;12;not-a-row")

    assert row is None
    assert "Invalid browse mouse row" in caplog.text


def test_browse_logs_invalid_mouse_sequence_decode(caplog):
    with caplog.at_level(logging.DEBUG, logger="voidx.ui.output.browse"):
        row = browse_module._mouse_row_from_sequence(b"\xff")

    assert row is None
    assert "Invalid browse mouse sequence" in caplog.text
