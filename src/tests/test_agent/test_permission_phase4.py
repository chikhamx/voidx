"""Phase 4 permission epoch, subagent snapshot, and LSP filtering tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.lsp.schema import LspLocation, LspPosition, LspRange
from voidx.permission.grants import AccessGrant, AccessGrants
from voidx.permission.service import PermissionService
from voidx.tools.agent import AgentTool
from voidx.tools.base import ToolContext
from voidx.tools.lsp import LspTool


class _AgentDef:
    name = "voidx"
    model = None


class _FakeLspManager:
    def __init__(self, workspace: str, *, references: list[LspLocation] | None = None) -> None:
        self.workspace = workspace
        self.references_result = references or []
        self.called = False

    async def definition(self, file_path: str, line: int, character: int):
        self.called = True
        raise AssertionError("definition should not be called for unauthorized input")

    async def references(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ):
        self.called = True
        return list(self.references_result)


def _range() -> LspRange:
    pos = LspPosition(line=0, character=0)
    return LspRange(start=pos, end=pos)


@pytest.mark.asyncio
async def test_main_tool_execution_lease_blocks_revocation(tmp_path):
    service = PermissionService(
        sandbox_readable_files=[str(tmp_path / "allowed.txt")],
    )
    lease = await service.acquire_execution_lease()
    before_epoch = service.revocation_epoch

    with pytest.raises(Exception, match="active execution lease"):
        service.set_permission_mode("read_only")

    assert service.permission_mode == "safe"
    assert service.revocation_epoch == before_epoch

    await lease.release()
    service.set_permission_mode("read_only")

    assert service.permission_mode == "read_only"
    assert service.revocation_epoch == before_epoch + 1


@pytest.mark.asyncio
async def test_pregranted_tool_holds_execution_lease(tmp_path):
    service = PermissionService(
        sandbox_writable_files=[str(tmp_path / "allowed.txt")],
    )

    async with service.execution_lease_for_tool("write") as lease:
        assert service.has_active_execution_lease(lease)
        with pytest.raises(Exception, match="active execution lease"):
            service.set_permission_mode("read_only")

    assert not service.has_active_execution_lease(lease)


def test_execution_lease_token_is_unforgeable():
    from voidx.permission.service import ExecutionLease

    with pytest.raises(TypeError):
        ExecutionLease()


@pytest.mark.asyncio
async def test_subagent_inherits_effective_grants(tmp_path):
    from voidx.permission.service import SubagentPermissionSnapshot

    service = PermissionService(
        sandbox_readable_files=[str(tmp_path / "readable.txt")],
    )
    writable = tmp_path / "writable.txt"
    await service.add_grant(AccessGrant(str(writable), "write", "file", "session"))

    snapshot = SubagentPermissionSnapshot.capture(service)
    grants = snapshot.get_access_grants(current_revocation_epoch=service.revocation_epoch)

    assert str(tmp_path / "readable.txt") in grants.readable_files
    assert str(writable) in grants.writable_files


def test_subagent_cannot_add_grant(tmp_path):
    from voidx.permission.service import SubagentPermissionSnapshot

    service = PermissionService()
    snapshot = SubagentPermissionSnapshot.capture(service)

    with pytest.raises(PermissionError, match="cannot add grant"):
        snapshot.add_grant(AccessGrant(str(tmp_path / "later.txt"), "read", "file", "session"))


@pytest.mark.asyncio
async def test_subagent_grants_snapshot_fixed(tmp_path):
    from voidx.permission.service import SubagentPermissionSnapshot

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    service = PermissionService(
        sandbox_readable_files=[str(first)],
    )
    snapshot = SubagentPermissionSnapshot.capture(service)

    await service.add_grant(AccessGrant(str(second), "read", "file", "session"))

    grants = snapshot.get_access_grants(current_revocation_epoch=service.revocation_epoch)
    assert str(first) in grants.readable_files
    assert str(second) not in grants.readable_files


def test_subagent_snapshot_invalidated_on_revocation(tmp_path):
    from voidx.permission.service import SubagentPermissionSnapshot

    service = PermissionService(
        sandbox_readable_files=[str(tmp_path / "readable.txt")],
    )
    snapshot = SubagentPermissionSnapshot.capture(service)

    service.set_permission_mode("read_only")

    with pytest.raises(PermissionError, match="revoked"):
        snapshot.get_access_grants(current_revocation_epoch=service.revocation_epoch)


@pytest.mark.asyncio
async def test_agent_tool_passes_subagent_permission_snapshot(tmp_path):
    captured: dict[str, object] = {}
    service = PermissionService(
        sandbox_readable_files=[str(tmp_path / "readable.txt")],
    )

    async def runner(agent_def, description, goal_resolution, result_contract, *, permission_snapshot=None):
        captured["snapshot"] = permission_snapshot
        return "done"

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: _AgentDef(),
        available_agents=["voidx"],
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        get_access_grants=service.get_access_grants,
        get_revocation_epoch=lambda: service.revocation_epoch,
    )

    result = await tool.execute(
        {
            "agent": "voidx",
            "mode": "review",
            "task": "Review permission snapshot propagation",
            "target": "src/voidx/tools/agent.py",
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    snapshot = captured["snapshot"]
    grants = snapshot.get_access_grants(current_revocation_epoch=service.revocation_epoch)
    assert str(tmp_path / "readable.txt") in grants.readable_files


@pytest.mark.asyncio
async def test_lsp_filters_external_locations(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = workspace / "source.py"
    allowed = workspace / "allowed.py"
    secret = external / "secret.py"
    source.write_text("value = target\n", encoding="utf-8")
    allowed.write_text("target = 1\n", encoding="utf-8")
    secret.write_text("target = 2\n", encoding="utf-8")
    manager = _FakeLspManager(
        str(workspace),
        references=[
            LspLocation(uri=allowed.as_uri(), path=str(allowed), range=_range()),
            LspLocation(uri=secret.as_uri(), path=str(secret), range=_range()),
        ],
    )
    ctx = ToolContext(
        workspace=str(workspace),
        lsp_manager=manager,
        get_access_grants=lambda: AccessGrants(),
    )

    result = await LspTool().execute(
        {"operation": "references", "file_path": str(source), "line": 1, "character": 8},
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert "allowed.py:1:0" in result.output
    assert "secret.py" not in result.output
    assert str(external) not in result.output
    assert "filtered" not in result.output.lower()


@pytest.mark.asyncio
async def test_lsp_input_requires_read_grant(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = external / "source.py"
    source.write_text("value = target\n", encoding="utf-8")
    manager = _FakeLspManager(str(workspace))
    ctx = ToolContext(
        workspace=str(workspace),
        lsp_manager=manager,
        get_access_grants=lambda: AccessGrants(),
    )

    result = await LspTool().execute(
        {"operation": "definition", "file_path": str(source), "line": 1, "character": 8},
        ctx,
    )

    assert result.metadata["error"] is True
    assert not manager.called
    assert str(source) not in result.output


def test_subagent_snapshot_checks_live_parent_revocation_epoch(tmp_path):
    from voidx.permission.service import SubagentPermissionSnapshot

    current_epoch = 0
    snapshot = SubagentPermissionSnapshot.from_parts(
        AccessGrants.from_parts(readable_files=[str(tmp_path / "allowed.txt")]),
        revocation_epoch=current_epoch,
        current_revocation_epoch=lambda: current_epoch,
    )

    assert str(tmp_path / "allowed.txt") in snapshot.get_access_grants().readable_files
    current_epoch = 1

    with pytest.raises(PermissionError, match="revoked"):
        snapshot.get_access_grants()


@pytest.mark.asyncio
async def test_permission_mode_change_blocked_by_execution_lease(tmp_path):
    service = PermissionService(permission_mode="safe")
    lease = await service.acquire_execution_lease()

    with pytest.raises(Exception, match="active execution lease"):
        service.set_permission_mode("full_access")

    assert service.permission_mode == "safe"

    await lease.release()
    service.set_permission_mode("full_access")

    assert service.permission_mode == "full_access"
    assert service.sandbox_mode == "danger-full-access"