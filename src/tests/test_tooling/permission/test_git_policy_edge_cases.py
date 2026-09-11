from pathlib import Path
import pytest
from voidx.tooling.policy.filesystem.grants import resolve_access
from voidx.tooling.policy.shell.policy import (
    _git_write_paths,
    _git_access_paths,
    _looks_like_path,
    shell_sandbox_precheck,
)
from voidx.tooling.domain.authorization import PermissionContext


def test_git_commit_message_not_treated_as_path():
    long_msg = (
        "fix(tui): 修复思考流区域索引错误并防止多余空行提交至滚动回卷\n\n"
        "- 将 render_frame.py 中 thinking_stream_rows 索引从 projected_regions[3] 修正为 [2]，"
        "匹配 thinking 区域真实高度，避免 busy activity 误判触发全帧重绘\n"
        "- 优化 _safe_flush_limit 逻辑，在未完成节点前或末尾无后续节点时拦截空行提交，"
        "同时保留 turn 与首个 assistant 消息间的合法分隔空行\n"
        "- 补充思考流行数追踪与空行提交边界的相关单测"
    )
    cmd = f'git commit -m "{long_msg}"'
    words = ["git", "commit", "-m", long_msg]
    write_paths = _git_write_paths(words)
    assert len(write_paths) == 0

    ctx = PermissionContext(workspace="/Users/chikham/workspace/voidx")
    # Should not raise OSError: [Errno 63] File name too long
    action, reason = shell_sandbox_precheck({"command": cmd}, ctx)
    assert action in {"allow", "defer"}


def test_resolve_access_handles_extremely_long_or_invalid_path():
    workspace = "/Users/chikham/workspace/voidx"
    long_filename = "a" * 400
    # On macOS this would normally throw [Errno 63] File name too long on is_dir() or exists()
    resolution = resolve_access(workspace, long_filename, access="read")
    assert resolution is not None
    assert resolution.action in {"allow", "defer", "deny"}

    resolution_write = resolve_access(workspace, long_filename, access="write")
    assert resolution_write is not None
    assert resolution_write.action in {"allow", "defer", "deny"}


def test_looks_like_path_rejects_newlines_and_too_long_segments():
    assert not _looks_like_path("hello\nworld.py")
    assert not _looks_like_path("a" * 300)
    assert _looks_like_path("src/foo.py")
    assert _looks_like_path("./bar.py")
    assert _looks_like_path("baz.py")


def test_git_status_and_ls_files_do_not_swallow_paths():
    status_words = ["git", "status", "-s", "src/foo.py"]
    access_paths = _git_access_paths(status_words)
    assert Path("src/foo.py") in access_paths

    ls_files_words = ["git", "ls-files", "-m", "src/bar.py"]
    access_paths = _git_access_paths(ls_files_words)
    assert Path("src/bar.py") in access_paths

    ls_files_others = ["git", "ls-files", "-o", "src/baz.py"]
    access_paths = _git_access_paths(ls_files_others)
    assert Path("src/baz.py") in access_paths
    write_paths = _git_write_paths(ls_files_others)
    assert len(write_paths) == 0


def test_git_format_patch_output_directory():
    words1 = ["git", "format-patch", "--output-directory", "/tmp/patches", "HEAD~1"]
    write_paths1 = _git_write_paths(words1)
    assert Path("/tmp/patches") in write_paths1

    words2 = ["git", "format-patch", "--output-directory=/tmp/patches", "HEAD~1"]
    write_paths2 = _git_write_paths(words2)
    assert Path("/tmp/patches") in write_paths2


def test_git_access_paths_captures_input_files():
    blame_words = ["git", "blame", "--contents", "/path/to/file", "target.py"]
    paths = _git_access_paths(blame_words)
    assert Path("/path/to/file") in paths
    assert Path("target.py") in paths

    ignore_words = ["git", "blame", "--ignore-revs-file=/path/to/revs", "target.py"]
    paths = _git_access_paths(ignore_words)
    assert Path("/path/to/revs") in paths

    ls_words = ["git", "ls-files", "--exclude-from", "/path/to/ignore"]
    paths = _git_access_paths(ls_words)
    assert Path("/path/to/ignore") in paths


def test_git_write_command_preserves_read_paths_in_shell_policy():
    from voidx.tooling.policy.shell.policy import _bash_policy, _powershell_policy
    words = ["git", "commit", "-F", "/path/to/msg.txt"]
    bash_policy = _bash_policy(words)
    assert Path("/path/to/msg.txt") in bash_policy.read_paths

    ps_policy = _powershell_policy(words)
    assert Path("/path/to/msg.txt") in ps_policy.read_paths
