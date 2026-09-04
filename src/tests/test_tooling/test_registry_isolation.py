"""Parent/child registry isolation for subagent runs.

A child registry must not share mutable plugin instances with its parent:
binding child runtimes or scoped services must leave the parent registry
bit-for-bit behaviorally identical.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.tool_registry import build_registry
from voidx.agent.adapters.tools.context import AgentToolRuntime
from voidx.agent.adapters.tools.plugins import AgentToolPlugin, bind_agent_tool_runtime
from voidx.tooling.adapters.scoped_plugin import (
    FileScopedPlugin,
    ShellScopedPlugin,
    bind_scoped_plugins,
)
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.domain.capability import ToolCapability
from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.domain.result import ToolResult


def test_child_copy_clones_mutable_wrapper_plugins() -> None:
    parent = build_registry()

    child = parent.child_copy()

    for tool_id in ("read", "write", "bash", "lsp", "todo", "workflow"):
        parent_plugin = parent.get(tool_id)
        child_plugin = child.get(tool_id)
        assert parent_plugin is not None
        assert child_plugin is not None
        assert child_plugin is not parent_plugin, f"{tool_id} instance leaked into child"


def test_child_copy_gives_todo_a_fresh_tracker() -> None:
    parent = build_registry()
    parent_plugin = parent.get("todo")
    assert isinstance(parent_plugin, AgentToolPlugin)
    parent_tracker = parent_plugin.tool._tracker

    child = parent.child_copy()

    child_plugin = child.get("todo")
    assert isinstance(child_plugin, AgentToolPlugin)
    assert child_plugin.tool is not parent_plugin.tool
    assert child_plugin.tool._tracker is not parent_tracker


def test_child_copy_shares_explicitly_shareable_plugins() -> None:
    parent = build_registry()

    child = parent.child_copy()

    for tool_id in ("document", "webfetch", "websearch", "mcp", "skill"):
        assert child.get(tool_id) is parent.get(tool_id), (
            f"{tool_id} is classified shareable and must stay shared"
        )


def test_child_copy_rejects_unclassified_plugin() -> None:
    class UnclassifiedPlugin:
        id = "mystery"
        description = "plugin without an explicit child classification"

        def parameters_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, args, ctx) -> ToolResult:
            return ToolResult(output="ok")

    registry = ToolRegistry(
        [UnclassifiedPlugin()],
        capabilities={"mystery": ToolCapability.ORCHESTRATION},
    )

    with pytest.raises(TypeError, match="mystery"):
        registry.child_copy()


def test_child_runtime_binding_leaves_parent_untouched() -> None:
    parent = build_registry()
    parent_runtime = AgentToolRuntime()
    bind_agent_tool_runtime(parent, parent_runtime)

    child = parent.child_copy()
    child_runtime = AgentToolRuntime(run_id="child-run")
    bind_agent_tool_runtime(child, child_runtime)

    parent_todo = parent.get("todo")
    child_todo = child.get("todo")
    assert isinstance(parent_todo, AgentToolPlugin)
    assert isinstance(child_todo, AgentToolPlugin)
    assert parent_todo.runtime is parent_runtime
    assert child_todo.runtime is child_runtime


def test_child_scoped_binding_leaves_parent_untouched() -> None:
    parent = build_registry()
    parent_auth = AuthorizationRuntime(read_dirs=["/parent"])
    parent_files = FileStateStore()
    bind_scoped_plugins(parent, authorization=parent_auth, files=parent_files)

    child = parent.child_copy()
    child_auth = AuthorizationRuntime(read_dirs=["/child"])
    child_files = FileStateStore()
    bind_scoped_plugins(child, authorization=child_auth, files=child_files)

    parent_read = parent.get("read")
    child_read = child.get("read")
    assert isinstance(parent_read, FileScopedPlugin)
    assert isinstance(child_read, FileScopedPlugin)
    assert parent_read.authorization is parent_auth
    assert parent_read.files is parent_files
    assert child_read.authorization is child_auth
    assert child_read.files is child_files

    parent_shell = parent.get("bash")
    child_shell = child.get("bash")
    assert isinstance(parent_shell, ShellScopedPlugin)
    assert isinstance(child_shell, ShellScopedPlugin)
    assert parent_shell.invoker is parent
    assert child_shell.invoker is child


def test_child_copy_filters_allowed_ids() -> None:
    parent = build_registry()

    child = parent.child_copy({"read", "search"})

    assert set(child.ids()) == {"read", "search"}
    assert parent.get("write") is not None


class _ProbeTool:
    """Captures the scoped authorization injected at execution time."""

    id = "probe"
    description = "probe"

    def __init__(self) -> None:
        self.seen: list[object] = []

    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args, ctx) -> ToolResult:
        await asyncio.sleep(0)
        self.seen.append(getattr(ctx, "authorization_service", None))
        return ToolResult(output="ok")


@pytest.mark.asyncio
async def test_parallel_parent_child_execution_uses_own_scoped_authorization() -> None:
    parent_auth = AuthorizationRuntime(read_dirs=["/parent"])
    parent = ToolRegistry(
        [FileScopedPlugin(_ProbeTool(), parent_auth, FileStateStore())],
        capabilities={"probe": ToolCapability.READ_ONLY},
    )
    bind_scoped_plugins(parent, authorization=parent_auth, files=FileStateStore())

    child = parent.child_copy()
    child_auth = AuthorizationRuntime(read_dirs=["/child"])
    bind_scoped_plugins(child, authorization=child_auth, files=FileStateStore())

    ctx = ToolExecutionContext(workspace="/tmp")
    parent_result, child_result = await asyncio.gather(
        parent.execute_tool("probe", {}, ctx),
        child.execute_tool("probe", {}, ctx),
    )

    assert parent_result.output == "ok"
    assert child_result.output == "ok"
    parent_probe = parent.get("probe")
    child_probe = child.get("probe")
    # Wrappers are isolated instances; the inner tool is stateless and shared by
    # design. Each side must observe only its own scoped authorization.
    assert parent_probe is not child_probe
    assert parent_probe.tool is child_probe.tool
    seen = parent_probe.tool.seen
    assert len(seen) == 2
    assert parent_auth in seen
    assert child_auth in seen
