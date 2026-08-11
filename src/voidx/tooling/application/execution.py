"""Explicit application services used by scoped Tooling plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ConfigDict, Field, SkipValidation

from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.domain.grants import AccessGrants, ObjectType
from voidx.tooling.ports.interaction import InteractionPort
from voidx.tooling.ports.post_edit import PostEditFormatter
from voidx.tooling.ports.invoker import ToolInvoker
from voidx.tooling.ports.process import ProcessSandbox


GrantWriter = Callable[..., object | Awaitable[object]]
GrantTargetLocker = Callable[..., object | Awaitable[object]]
ExecutionLeaseFactory = Callable[[str], object]
CreatedPathRecorder = Callable[..., object | Awaitable[object]]
CreatedPathForgetter = Callable[..., object | Awaitable[object]]
CreatedPathMover = Callable[..., object | Awaitable[object]]


@dataclass
class AuthorizationRuntime:
    read_files: list[str] = field(default_factory=list)
    read_dirs: list[str] = field(default_factory=list)
    write_files: list[str] = field(default_factory=list)
    write_dirs: list[str] = field(default_factory=list)
    access_grants_reader: Callable[[], AccessGrants] | None = None
    revocation_epoch_reader: Callable[[], int] | None = None
    grant_writer: GrantWriter | None = None
    target_locker: GrantTargetLocker | None = None
    execution_lease_factory: ExecutionLeaseFactory | None = None
    interaction: InteractionPort | None = None
    created_path_recorder: CreatedPathRecorder | None = None
    created_path_forgetter: CreatedPathForgetter | None = None
    created_path_mover: CreatedPathMover | None = None

    def access_grants(self) -> AccessGrants:
        if self.access_grants_reader is not None:
            return self.access_grants_reader()
        return AccessGrants.from_parts(
            readable_files=self.read_files,
            readable_dirs=self.read_dirs,
            writable_files=self.write_files,
            writable_dirs=self.write_dirs,
        )

    def sandbox_paths(self, *, write: bool) -> list[str]:
        writable = [*self.write_files, *self.write_dirs]
        return writable if write else [*self.read_files, *self.read_dirs, *writable]

    async def record_created_path(
        self,
        workspace: str,
        path: str | Path,
        *,
        object_type: ObjectType,
    ) -> None:
        if self.created_path_recorder is None or _is_workspace_path(workspace, path):
            return
        await _maybe_await(self.created_path_recorder(path, object_type=object_type))

    async def forget_created_path(
        self,
        workspace: str,
        path: str | Path,
        *,
        object_type: ObjectType,
    ) -> None:
        if self.created_path_forgetter is None or _is_workspace_path(workspace, path):
            return
        await _maybe_await(self.created_path_forgetter(path, object_type=object_type))

    async def move_created_path(
        self,
        workspace: str,
        source: str | Path,
        dest: str | Path,
        *,
        object_type: ObjectType,
        destination_created: bool = False,
    ) -> None:
        source_external = not _is_workspace_path(workspace, source)
        dest_external = not _is_workspace_path(workspace, dest)
        if source_external and dest_external and self.created_path_mover is not None:
            await _maybe_await(
                self.created_path_mover(
                    source,
                    dest,
                    object_type=object_type,
                    destination_created=destination_created,
                )
            )
        elif source_external and not dest_external and self.created_path_forgetter is not None:
            await _maybe_await(
                self.created_path_forgetter(source, object_type=object_type)
            )
        elif (
            not source_external
            and dest_external
            and destination_created
            and self.created_path_recorder is not None
        ):
            await _maybe_await(
                self.created_path_recorder(dest, object_type=object_type)
            )


async def _maybe_await(value: object) -> object:
    return await value if isinstance(value, Awaitable) else value


def _is_workspace_path(workspace: str, path: str | Path) -> bool:
    try:
        workspace_path = Path(workspace).expanduser().resolve()
        target = Path(path).expanduser().resolve(strict=False)
        return target == workspace_path or target.is_relative_to(workspace_path)
    except (OSError, RuntimeError, ValueError):
        return False


class CallbackInteractionPort:
    def __init__(self, callback: Callable[..., object]) -> None:
        self._callback = callback

    async def request(self, interaction):
        result = self._callback(interaction)
        return await result if isinstance(result, Awaitable) else result


class FileToolContext(ToolExecutionContext):
    authorization_service: SkipValidation[AuthorizationRuntime] = Field(default_factory=AuthorizationRuntime)
    file_state: SkipValidation[FileStateStore] = Field(default_factory=FileStateStore)
    post_edit_formatter: SkipValidation[PostEditFormatter | None] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ShellToolContext(FileToolContext):
    process_sandbox: SkipValidation[ProcessSandbox | None] = None
    tool_invoker: SkipValidation[ToolInvoker | None] = None


__all__ = ["AuthorizationRuntime", "FileStateStore", "FileToolContext", "ShellToolContext"]
