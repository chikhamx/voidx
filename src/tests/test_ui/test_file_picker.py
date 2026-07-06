"""Tests for file_picker sorting: mtime descending across all entry types."""

import os
import time
from pathlib import Path

import pytest

from voidx.ui.tools.file_picker import FileCandidate, list_file_candidates


@pytest.fixture()
def workspace(tmp_path: Path):
    """Create a workspace with files/dirs of known mtimes."""
    # Directory entries
    (tmp_path / "old_dir").mkdir()
    (tmp_path / "new_dir").mkdir()

    # File entries
    (tmp_path / "old_file.py").write_text("old")
    (tmp_path / "new_file.py").write_text("new")
    (tmp_path / "mid_file.py").write_text("mid")

    # Set mtimes: old=100, mid=200, new=300
    base = tmp_path
    os.utime(base / "old_dir", (100, 100))
    os.utime(base / "old_file.py", (100, 100))
    os.utime(base / "mid_file.py", (200, 200))
    os.utime(base / "new_dir", (300, 300))
    os.utime(base / "new_file.py", (300, 300))

    return str(base)


class TestMtimeSorting:
    def test_mtime_descending_within_dirs(self, workspace: str):
        """Directories should be sorted by mtime descending (newest first)."""
        results = list_file_candidates(workspace, "", limit=20)
        dirs = [c for c in results if c.kind == "dir"]
        assert len(dirs) == 2
        # new_dir (mtime=300) should come before old_dir (mtime=100)
        assert dirs[0].rel_path == "new_dir/"
        assert dirs[1].rel_path == "old_dir/"

    def test_mtime_descending_within_files(self, workspace: str):
        """Files should be sorted by mtime descending (newest first)."""
        results = list_file_candidates(workspace, "", limit=20)
        files = [c for c in results if c.kind != "dir"]
        assert len(files) == 3
        # new_file (300) > mid_file (200) > old_file (100)
        assert files[0].rel_path == "new_file.py"
        assert files[1].rel_path == "mid_file.py"
        assert files[2].rel_path == "old_file.py"

    def test_mtime_descending_across_types(self, workspace: str):
        """Dirs and files should be interleaved by mtime, not dirs-first."""
        results = list_file_candidates(workspace, "", limit=20)
        # Expected order by mtime descending: new_dir(300), new_file(300), mid_file(200), old_dir(100), old_file(100)
        paths = [c.rel_path for c in results]
        assert paths.index("new_dir/") < paths.index("mid_file.py")
        assert paths.index("mid_file.py") < paths.index("old_dir/")
        assert paths.index("old_dir/") < paths.index("old_file.py")

    def test_candidate_has_mtime(self, workspace: str):
        """FileCandidate should carry mtime info."""
        results = list_file_candidates(workspace, "", limit=1)
        assert hasattr(results[0], "mtime")
        assert results[0].mtime > 0
