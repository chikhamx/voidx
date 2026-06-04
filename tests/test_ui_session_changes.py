from voidx.ui.output.diff import make_file_diff
from voidx.ui.session import SessionChangeTracker, session_tracker


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


def test_change_summary_lines_empty_before_finish(tmp_path):
    tracker = SessionChangeTracker()
    tracker.begin_turn(str(tmp_path))
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    tracker.capture_file("a.py", str(tmp_path))
    tracker.record_diff(make_file_diff("a.py", "old\n", "new\n"))
    assert tracker.change_summary_lines() == []


def test_change_summary_lines_no_changes(tmp_path):
    tracker = SessionChangeTracker()
    tracker.begin_turn(str(tmp_path))
    tracker.finish_turn()
    assert tracker.change_summary_lines() == []


def test_change_summary_lines_modified_file(tmp_path):
    tracker = SessionChangeTracker()
    tracker.begin_turn(str(tmp_path))
    (tmp_path / "existing.py").write_text("old\n", encoding="utf-8")
    tracker.capture_file("existing.py", str(tmp_path))
    tracker.record_diff(make_file_diff("existing.py", "old\n", "new\n"))
    tracker.finish_turn()
    lines = tracker.change_summary_lines()
    assert len(lines) == 1
    assert "existing.py" in lines[0]
    assert "Modified" in lines[0]


def test_change_summary_lines_created_file(tmp_path):
    tracker = SessionChangeTracker()
    tracker.begin_turn(str(tmp_path))
    new_file = tmp_path / "new.py"
    tracker.capture_file("new.py", str(tmp_path))
    new_file.write_text("hello\n", encoding="utf-8")
    tracker.record_diff(make_file_diff("new.py", "", "hello\n", old_label="/dev/null", new_label="b/new.py"))
    tracker.finish_turn()
    lines = tracker.change_summary_lines()
    assert len(lines) == 1
    assert "new.py" in lines[0]
    assert "Created" in lines[0]


def test_change_summary_lines_mixed(tmp_path):
    tracker = SessionChangeTracker()
    tracker.begin_turn(str(tmp_path))
    (tmp_path / "mod.py").write_text("old\n", encoding="utf-8")
    tracker.capture_file("mod.py", str(tmp_path))
    tracker.record_diff(make_file_diff("mod.py", "old\n", "new\n"))
    new_file = tmp_path / "new.py"
    tracker.capture_file("new.py", str(tmp_path))
    new_file.write_text("hello\n", encoding="utf-8")
    tracker.record_diff(make_file_diff("new.py", "", "hello\n", old_label="/dev/null", new_label="b/new.py"))
    tracker.finish_turn()
    lines = tracker.change_summary_lines()
    assert len(lines) == 2
    paths = [l for l in lines if "mod.py" in l or "new.py" in l]
    assert len(paths) == 2
    mod_line = [l for l in lines if "mod.py" in l][0]
    new_line = [l for l in lines if "new.py" in l][0]
    assert "Modified" in mod_line
    assert "Created" in new_line
