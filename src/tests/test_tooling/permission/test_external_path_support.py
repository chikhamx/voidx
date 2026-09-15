"""External path support: dynamic grants in sandbox_paths, search/find intake, shell intents."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidx.tooling.application.authorization import authorize_tool_call, sandbox_precheck_action
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.domain.grants import AccessGrants
from voidx.tooling.policy.permission.rules import classify_tool_call, file_paths_for_tool
from voidx.tooling.policy.shell.policy import shell_sandbox_precheck, shell_sandbox_precheck_with_intents


def _runtime(grants: AccessGrants) -> AuthorizationRuntime:
    return AuthorizationRuntime(access_grants_reader=lambda: grants)


class TestSandboxPathsDynamicGrants:
    def test_read_paths_include_session_grants(self, tmp_path):
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        grants = AccessGrants.from_parts(readable_dirs=[str(external_dir)])
        runtime = _runtime(grants)

        paths = runtime.sandbox_paths(write=False)

        assert str(external_dir) in paths

    def test_write_paths_include_writable_grants(self, tmp_path):
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        grants = AccessGrants.from_parts(writable_dirs=[str(external_dir)])
        runtime = _runtime(grants)

        write_paths = runtime.sandbox_paths(write=True)
        read_paths = runtime.sandbox_paths(write=False)

        assert str(external_dir) in write_paths
        assert str(external_dir) in read_paths

    def test_static_lists_still_present_and_deduplicated(self, tmp_path):
        shared = str((tmp_path / "shared").resolve())
        runtime = AuthorizationRuntime(
            read_dirs=[shared],
            write_dirs=[shared],
            access_grants_reader=lambda: AccessGrants.from_parts(
                readable_dirs=[shared], writable_dirs=[shared]
            ),
        )

        assert runtime.sandbox_paths(write=False).count(shared) == 1
        assert runtime.sandbox_paths(write=True).count(shared) == 1


def _context(workspace: Path, **overrides) -> PermissionContext:
    base = dict(workspace=str(workspace), interaction_mode="auto", permission_mode="safe")
    base.update(overrides)
    return PermissionContext(**base)


class TestSearchFindPermissionIntake:
    def test_file_paths_for_tool_extracts_path_arg(self):
        assert file_paths_for_tool("search", {"query": "x", "path": "/external"}) == ["/external"]
        assert file_paths_for_tool("find", {"extensions": ["py"], "path": "/external"}) == ["/external"]
        assert file_paths_for_tool("search", {"query": "x"}) == []

    def test_search_external_path_defers_with_read_intent(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        classified = classify_tool_call({"name": "search", "args": {"query": "x", "path": str(external)}})
        action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

        assert action == "defer"
        assert len(intents) == 1
        assert intents[0].access == "read"
        assert intents[0].is_workspace_path is False

    def test_find_external_path_defers_with_read_intent(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        classified = classify_tool_call({"name": "find", "args": {"extensions": ["py"], "path": str(external)}})
        action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

        assert action == "defer"
        assert len(intents) == 1
        assert intents[0].access == "read"

    def test_search_workspace_path_allowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        classified = classify_tool_call({"name": "search", "args": {"query": "x", "path": str(workspace)}})
        action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

        assert action == "allow"
        assert intents == ()

    def test_search_default_path_allowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        classified = classify_tool_call({"name": "search", "args": {"query": "x"}})
        action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

        assert action == "allow"

    def test_search_granted_external_path_allowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        grants = AccessGrants.from_parts(readable_dirs=[str(external.resolve())])

        classified = classify_tool_call({"name": "search", "args": {"query": "x", "path": str(external)}})
        action, reason, intents = sandbox_precheck_action(classified, _context(workspace, access_grants=grants))

        assert action == "allow"
        assert intents == ()


class TestShellSandboxPrecheckIntents:
    def test_with_intents_collects_external_read_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        action, reason, intents = shell_sandbox_precheck_with_intents(
            {"command": f"git -C {external} log -n 5"},
            _context(workspace),
            shell="bash",
        )

        assert action == "defer"
        assert "external path requires" in (reason or "")
        assert len(intents) == 1
        assert intents[0].access == "read"
        assert not intents[0].is_workspace_path

    def test_with_intents_granted_path_allows(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        grants = AccessGrants.from_parts(readable_dirs=[str(external.resolve())])

        action, reason, intents = shell_sandbox_precheck_with_intents(
            {"command": f"git -C {external} log -n 5"},
            _context(workspace, access_grants=grants),
            shell="bash",
        )

        assert action == "allow"
        assert intents == ()

    def test_legacy_signature_unchanged(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        result = shell_sandbox_precheck(
            {"command": f"git -C {external} log -n 5"},
            _context(workspace),
            shell="bash",
        )

        assert isinstance(result, tuple)
        assert len(result) == 2
        action, reason = result
        assert action == "defer"
        assert "external path requires" in (reason or "")

    def test_precheck_action_propagates_shell_intents(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        classified = classify_tool_call({"name": "bash", "args": {"command": f"git -C {external} status"}})
        action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

        assert action == "defer"
        assert len(intents) == 1
        assert intents[0].access == "read"


class TestSessionRuleShortCircuitGuard:
    def test_session_allow_does_not_bypass_unauthorized_external_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        command = f"git -C {external} status"

        decision = authorize_tool_call(
            {"name": "bash", "args": {"command": command}},
            _context(workspace, session_allow=frozenset({command})),
        )

        assert decision.action in {"ask", "blocked_ack"}
        assert len(decision.access_intents) == 1

    def test_session_allow_still_short_circuits_without_external_paths(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        command = "git status"

        decision = authorize_tool_call(
            {"name": "bash", "args": {"command": command}},
            _context(workspace, session_allow=frozenset({command})),
        )

        assert decision.action == "allow"


class TestMixedReadWriteIntents:
    def test_read_and_write_intents_collected_in_one_pass(self, tmp_path):
        workspace = tmp_path / "workspace"
        ext_read = tmp_path / "ext_read"
        ext_write = tmp_path / "ext_write"
        workspace.mkdir()
        ext_read.mkdir()
        ext_write.mkdir()
        (ext_read / "in.txt").write_text("data", encoding="utf-8")

        action, reason, intents = shell_sandbox_precheck_with_intents(
            {"command": f"cp {ext_read}/in.txt {ext_write}/out.txt"},
            _context(workspace),
            shell="bash",
        )

        assert action == "defer"
        accesses = {intent.access for intent in intents}
        assert accesses == {"read", "write"}
        # bash/tool.py gatekeeper matches on this substring
        assert "external path requires" in (reason or "")
