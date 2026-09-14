from pathlib import Path
from voidx.platform.paths import workspace_candidates


def test_workspace_candidates_basic(tmp_path):
    ws = str(tmp_path / "my_project")
    candidates = workspace_candidates(ws)
    assert ws in candidates
    resolved = str(Path(ws).resolve())
    assert resolved in candidates


def test_workspace_candidates_home_and_tilde():
    home = str(Path.home())
    sub = f"{home}/test_repo"
    candidates_from_abs = workspace_candidates(sub)
    assert sub in candidates_from_abs
    assert "~/test_repo" in candidates_from_abs

    candidates_from_tilde = workspace_candidates("~/test_repo")
    assert "~/test_repo" in candidates_from_tilde
    assert sub in candidates_from_tilde


def test_workspace_candidates_empty_or_none():
    assert workspace_candidates("") == []
