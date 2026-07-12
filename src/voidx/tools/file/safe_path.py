from __future__ import annotations

import errno
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Access = Literal["read", "write"]
_TOKEN = object()


@dataclass(frozen=True)
class SafePathResult:
    ok: bool
    value: str | bytes | None = None
    error: str = ""
    error_kind: str = ""


class AuthorizedPath:
    __slots__ = (
        "_path",
        "_access",
        "_stat",
        "_parent_path",
        "_parent_stat",
        "_owner",
        "_marker",
        "_sealed",
    )

    def __init__(
        self,
        path: Path,
        access: Access,
        stat_result: os.stat_result | None,
        parent_path: Path,
        parent_stat: os.stat_result,
        owner: "SafePathExecutor",
        token: object,
    ) -> None:
        if token is not _TOKEN:
            raise TypeError("AuthorizedPath cannot be constructed directly")
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_access", access)
        object.__setattr__(self, "_stat", stat_result)
        object.__setattr__(self, "_parent_path", parent_path)
        object.__setattr__(self, "_parent_stat", parent_stat)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_marker", _TOKEN)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("AuthorizedPath is immutable")
        object.__setattr__(self, name, value)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def access(self) -> Access:
        return self._access


class SafePathExecutor:
    def authorize_existing(self, path: str | Path, *, access: Access) -> AuthorizedPath:
        target = Path(path).expanduser().resolve(strict=False)
        stat_result = _stat_no_symlink(target)
        if stat_result is None:
            raise OSError(errno.ENOENT, "path does not exist", str(target))
        if _is_symlink(stat_result):
            raise OSError(errno.ELOOP, "path is a symbolic link", str(target))
        parent_path, parent_stat = _parent_record(target)
        return AuthorizedPath(target, access, stat_result, parent_path, parent_stat, self, _TOKEN)

    def authorize_target(self, path: str | Path, *, access: Access) -> AuthorizedPath:
        target = Path(path).expanduser().resolve(strict=False)
        stat_result = _stat_no_symlink(target)
        if stat_result is not None and _is_symlink(stat_result):
            raise OSError(errno.ELOOP, "path is a symbolic link", str(target))
        parent_path, parent_stat = _parent_record(target)
        return AuthorizedPath(target, access, stat_result, parent_path, parent_stat, self, _TOKEN)

    def read_text(self, authorized: AuthorizedPath, *, encoding: str = "utf-8", errors: str = "replace") -> SafePathResult:
        invalid = self._validate_capability(authorized)
        if invalid:
            return invalid
        if authorized.access not in {"read", "write"}:
            return SafePathResult(False, error="Authorized path is not readable", error_kind="wrong_access")
        changed = _path_changed(authorized)
        if changed:
            return changed
        try:
            fd = _open_no_follow(authorized.path, os.O_RDONLY)
            try:
                opened_stat = os.fstat(fd)
                if authorized._stat is None or not _same_stat(authorized._stat, opened_stat):
                    return SafePathResult(False, error="Authorized path changed after authorization", error_kind="path_changed")
                with os.fdopen(fd, "r", encoding=encoding, errors=errors) as handle:
                    fd = -1
                    return SafePathResult(True, value=handle.read())
            finally:
                if fd != -1:
                    os.close(fd)
        except OSError as exc:
            return SafePathResult(False, error=str(exc), error_kind="io_error")

    def read_bytes_prefix(self, authorized: AuthorizedPath, size: int) -> SafePathResult:
        invalid = self._validate_capability(authorized)
        if invalid:
            return invalid
        if authorized.access not in {"read", "write"}:
            return SafePathResult(False, error="Authorized path is not readable", error_kind="wrong_access")
        changed = _path_changed(authorized)
        if changed:
            return changed
        try:
            fd = _open_no_follow(authorized.path, os.O_RDONLY)
            try:
                opened_stat = os.fstat(fd)
                if authorized._stat is None or not _same_stat(authorized._stat, opened_stat):
                    return SafePathResult(False, error="Authorized path changed after authorization", error_kind="path_changed")
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    return SafePathResult(True, value=handle.read(size))
            finally:
                if fd != -1:
                    os.close(fd)
        except OSError as exc:
            return SafePathResult(False, error=str(exc), error_kind="io_error")

    def write_text(self, authorized: AuthorizedPath, content: str, *, encoding: str = "utf-8") -> SafePathResult:
        invalid = self._validate_capability(authorized)
        if invalid:
            return invalid
        if authorized.access != "write":
            return SafePathResult(False, error="Authorized path is not writable", error_kind="wrong_access")
        changed = _parent_or_existing_path_changed(authorized)
        if changed:
            return changed
        temp_path: Path | None = None
        try:
            _mkdir_parent_safely(authorized.path.parent, authorized._parent_path, authorized._parent_stat)
            data = content.encode(encoding)
            temp_path = _atomic_temp_path(authorized.path.parent, authorized.path.name)
            fd = _open_no_follow(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            changed = _parent_or_existing_path_changed(authorized)
            if changed:
                return changed
            os.replace(temp_path, authorized.path)
            temp_path = None
            _fsync_directory(authorized.path.parent)
            return SafePathResult(True)
        except FileExistsError:
            return SafePathResult(False, error="Authorized target appeared after authorization", error_kind="path_changed")
        except OSError as exc:
            return SafePathResult(False, error=str(exc), error_kind=_mutation_error_kind(exc))
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def delete_file(self, authorized: AuthorizedPath) -> SafePathResult:
        invalid = self._validate_capability(authorized)
        if invalid:
            return invalid
        if authorized.access != "write":
            return SafePathResult(False, error="Authorized path is not writable", error_kind="wrong_access")
        changed = _path_changed(authorized)
        if changed:
            return changed
        try:
            authorized.path.unlink()
            return SafePathResult(True)
        except OSError as exc:
            return SafePathResult(False, error=str(exc), error_kind="io_error")

    def create_dir(self, authorized: AuthorizedPath) -> SafePathResult:
        invalid = self._validate_capability(authorized)
        if invalid:
            return invalid
        if authorized.access != "write":
            return SafePathResult(False, error="Authorized path is not writable", error_kind="wrong_access")
        changed = _parent_or_existing_path_changed(authorized)
        if changed:
            return changed
        try:
            _mkdir_parent_safely(authorized.path.parent, authorized._parent_path, authorized._parent_stat)
            authorized.path.mkdir()
            return SafePathResult(True)
        except FileExistsError:
            return SafePathResult(False, error="Authorized target appeared after authorization", error_kind="path_changed")
        except OSError as exc:
            return SafePathResult(False, error=str(exc), error_kind=_mutation_error_kind(exc))

    def delete_tree(self, authorized: AuthorizedPath) -> SafePathResult:
        invalid = self._validate_capability(authorized)
        if invalid:
            return invalid
        if authorized.access != "write":
            return SafePathResult(False, error="Authorized path is not writable", error_kind="wrong_access")
        changed = _path_changed(authorized)
        if changed:
            return changed
        try:
            _remove_tree(authorized.path, authorized._stat)
            return SafePathResult(True)
        except OSError as exc:
            return SafePathResult(False, error=str(exc), error_kind=_mutation_error_kind(exc))

    def rename(self, source: AuthorizedPath, dest: AuthorizedPath, *, overwrite: bool) -> SafePathResult:
        invalid = self._validate_capability(source) or self._validate_capability(dest)
        if invalid:
            return invalid
        if source.access != "write" or dest.access != "write":
            return SafePathResult(False, error="Rename requires write authorization for source and destination", error_kind="wrong_access")
        source_changed = _path_changed(source)
        if source_changed:
            return source_changed
        dest_changed = _parent_or_existing_path_changed(dest)
        if dest_changed:
            return dest_changed
        if dest.path.exists() and not overwrite:
            return SafePathResult(False, error="destination already exists", error_kind="destination_exists")
        try:
            _mkdir_parent_safely(dest.path.parent, dest._parent_path, dest._parent_stat)
            if not overwrite:
                if source._stat is not None and stat.S_ISDIR(source._stat.st_mode):
                    return _rename_dir_no_overwrite(source.path, dest.path, source._stat)
                return _rename_no_overwrite(source.path, dest.path, source._stat)
            os.replace(source.path, dest.path)
            return SafePathResult(True)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return SafePathResult(False, error="Cross-filesystem move is not supported", error_kind="cross_device_move")
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                return SafePathResult(False, error="destination already exists", error_kind="destination_exists")
            return SafePathResult(False, error=str(exc), error_kind=_mutation_error_kind(exc))

    def _validate_capability(self, authorized: object) -> SafePathResult | None:
        if not isinstance(authorized, AuthorizedPath):
            return SafePathResult(False, error="Invalid authorized path capability", error_kind="invalid_capability")
        if authorized._marker is not _TOKEN or authorized._owner is not self:
            return SafePathResult(False, error="Invalid authorized path capability", error_kind="invalid_capability")
        return None




def _atomic_temp_path(parent: Path, name: str) -> Path:
    for _ in range(100):
        candidate = parent / f".{name}.tmp-{secrets.token_hex(8)}"
        if not candidate.exists():
            return candidate
    raise OSError(errno.EEXIST, "could not allocate temporary path", str(parent / name))


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
def _open_no_follow(path: Path, flags: int) -> int:
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o666)


def _stat_no_symlink(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and left.st_mode == right.st_mode


def _is_symlink(stat_result: os.stat_result) -> bool:
    return stat.S_ISLNK(stat_result.st_mode)


def _is_directory(stat_result: os.stat_result) -> bool:
    return stat.S_ISDIR(stat_result.st_mode)


def _parent_record(path: Path) -> tuple[Path, os.stat_result]:
    parent = path.parent
    while True:
        stat_result = _stat_no_symlink(parent)
        if stat_result is not None:
            if _is_symlink(stat_result) or not _is_directory(stat_result):
                raise OSError(errno.ELOOP, "unsafe parent path", str(parent))
            return parent, stat_result
        if parent == parent.parent:
            raise OSError(errno.ENOENT, "no existing parent path", str(path))
        parent = parent.parent


def _parent_changed(authorized: AuthorizedPath) -> SafePathResult | None:
    current = _stat_no_symlink(authorized._parent_path)
    if (
        current is None
        or _is_symlink(current)
        or not _is_directory(current)
        or not _same_stat(authorized._parent_stat, current)
    ):
        return SafePathResult(False, error="Authorized parent path changed after authorization", error_kind="path_changed")
    return None


def _path_changed(authorized: AuthorizedPath) -> SafePathResult | None:
    parent_changed = _parent_changed(authorized)
    if parent_changed:
        return parent_changed
    current = _stat_no_symlink(authorized.path)
    if authorized._stat is None or current is None or _is_symlink(current) or not _same_stat(authorized._stat, current):
        return SafePathResult(False, error="Authorized path changed after authorization", error_kind="path_changed")
    return None


def _parent_or_existing_path_changed(authorized: AuthorizedPath) -> SafePathResult | None:
    parent_changed = _parent_changed(authorized)
    if parent_changed:
        return parent_changed
    current = _stat_no_symlink(authorized.path)
    if authorized._stat is not None:
        if current is None or _is_symlink(current) or not _same_stat(authorized._stat, current):
            return SafePathResult(False, error="Authorized path changed after authorization", error_kind="path_changed")
    elif current is not None:
        return SafePathResult(False, error="Authorized target appeared after authorization", error_kind="path_changed")
    return None


def _mkdir_parent_safely(parent: Path, pinned_parent: Path, pinned_parent_stat: os.stat_result) -> None:
    pinned_current = _stat_no_symlink(pinned_parent)
    if (
        pinned_current is None
        or _is_symlink(pinned_current)
        or not _is_directory(pinned_current)
        or not _same_stat(pinned_parent_stat, pinned_current)
    ):
        raise OSError(errno.EAGAIN, "authorized parent changed", str(pinned_parent))
    try:
        relative = parent.relative_to(pinned_parent)
    except ValueError as exc:
        raise OSError(errno.EPERM, "target parent escaped authorized parent", str(parent)) from exc
    current = pinned_parent
    for part in relative.parts:
        current = current / part
        stat_result = _stat_no_symlink(current)
        if stat_result is None:
            current.mkdir()
            stat_result = _stat_no_symlink(current)
        if stat_result is None or _is_symlink(stat_result) or not _is_directory(stat_result):
            raise OSError(errno.ELOOP, "unsafe parent path", str(current))
        verified = _stat_no_symlink(current)
        if verified is None or _is_symlink(verified) or not _same_stat(stat_result, verified):
            raise OSError(errno.EAGAIN, "parent path changed during creation", str(current))


def _remove_tree(path: Path, expected_stat: os.stat_result | None) -> None:
    if expected_stat is None:
        raise OSError(errno.EAGAIN, "directory path changed during removal", str(path))
    root_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened_stat = os.fstat(root_fd)
        if _is_symlink(opened_stat) or not _is_directory(opened_stat) or not _same_stat(expected_stat, opened_stat):
            raise OSError(errno.EAGAIN, "directory path changed during removal", str(path))
        entries = os.listdir(root_fd)
        for name in entries:
            stat_result = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if _is_symlink(stat_result):
                raise OSError(errno.ELOOP, "unsafe child path", str(path / name))
            verified = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not _same_stat(stat_result, verified):
                raise OSError(errno.EAGAIN, "child path changed during removal", str(path / name))
            if _is_directory(stat_result):
                _remove_tree(path / name, stat_result)
            else:
                os.unlink(name, dir_fd=root_fd)
        final_stat = os.fstat(root_fd)
        if not _same_stat(expected_stat, final_stat):
            raise OSError(errno.EAGAIN, "directory path changed during removal", str(path))
    finally:
        os.close(root_fd)
    path.rmdir()


def _rename_no_overwrite(source: Path, dest: Path, source_stat: os.stat_result | None) -> SafePathResult:
    if source_stat is None:
        return SafePathResult(False, error="Authorized source changed after authorization", error_kind="path_changed")
    if not stat.S_ISREG(source_stat.st_mode):
        return SafePathResult(False, error="No-overwrite directory move is not supported", error_kind="no_overwrite_unsupported")
    try:
        os.link(source, dest, follow_symlinks=False)
        dest_stat = _stat_no_symlink(dest)
        source_current = _stat_no_symlink(source)
        if (
            dest_stat is None
            or source_current is None
            or _is_symlink(dest_stat)
            or _is_symlink(source_current)
            or not _same_stat(source_stat, dest_stat)
            or not _same_stat(source_stat, source_current)
        ):
            try:
                dest.unlink()
            except OSError:
                pass
            return SafePathResult(False, error="Authorized path changed after authorization", error_kind="path_changed")
        source.unlink()
        return SafePathResult(True)
    except FileExistsError:
        return SafePathResult(False, error="destination already exists", error_kind="destination_exists")
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return SafePathResult(False, error="Cross-filesystem move is not supported", error_kind="cross_device_move")
        return SafePathResult(False, error=str(exc), error_kind=_mutation_error_kind(exc))


def _rename_dir_no_overwrite(source: Path, dest: Path, source_stat: os.stat_result) -> SafePathResult:
    try:
        os.rename(source, dest)
        dest_stat = _stat_no_symlink(dest)
        if dest_stat is None or _is_symlink(dest_stat) or not _same_stat(source_stat, dest_stat):
            return SafePathResult(False, error="Authorized path changed after authorization", error_kind="path_changed")
        return SafePathResult(True)
    except FileExistsError:
        return SafePathResult(False, error="destination already exists", error_kind="destination_exists")
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return SafePathResult(False, error="Cross-filesystem move is not supported", error_kind="cross_device_move")
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            return SafePathResult(False, error="destination already exists", error_kind="destination_exists")
        return SafePathResult(False, error=str(exc), error_kind=_mutation_error_kind(exc))


def _mutation_error_kind(exc: OSError) -> str:
    if exc.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EPERM, errno.EACCES, errno.EAGAIN}:
        return "path_changed"
    return "io_error"
