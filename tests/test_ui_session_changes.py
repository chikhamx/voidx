from voidx.ui.diff import make_file_diff
from voidx.ui.session_changes import session_tracker


def test_session_change_tracker_rolls_back_turn_files(tmp_path):
    session_tracker.clear()
    session_tracker.begin_turn(str(tmp_path))

    existing = tmp_path / "existing.py"
    existing.write_text("old\n", encoding="utf-8")
    new_file = tmp_path / "new.py"

    session_tracker.capture_file("existing.py", str(tmp_path))
    session_tracker.capture_file("new.py", str(tmp_path))

    existing.write_text("new\n", encoding="utf-8")
    new_file.write_text("hello\n", encoding="utf-8")

    session_tracker.record_diff(make_file_diff("existing.py", "old\n", "new\n"))
    session_tracker.record_diff(make_file_diff("new.py", "", "hello\n", old_label="/dev/null", new_label="b/new.py"))
    session_tracker.finish_turn()

    assert session_tracker.has_changes is True

    result = session_tracker.rollback_current()

    assert result.ok
    assert existing.read_text(encoding="utf-8") == "old\n"
    assert not new_file.exists()
    assert session_tracker.has_changes is False
    assert session_tracker.files == []
