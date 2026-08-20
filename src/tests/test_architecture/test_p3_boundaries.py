from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from .import_graph import PACKAGE_ROOT, ROOT, format_edges, import_edges, is_under, top_level


TOOLING_CORE = (
    "voidx.tooling.domain",
    "voidx.tooling.ports",
    "voidx.tooling.application",
    "voidx.tooling.policy",
    "voidx.tooling.builtin",
)


def _module_imports(module: str) -> list[str]:
    return [edge.target for edge in import_edges() if edge.source == module]


def _function_definitions(name: str) -> list[Path]:
    matches: list[Path] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name for node in ast.walk(tree)):
            matches.append(path.relative_to(ROOT))
    return matches


def test_tooling_core_does_not_import_agent_presentation_config_mcp_or_lsp() -> None:
    violations = [
        edge
        for edge in import_edges()
        if any(is_under(edge.source, prefix) for prefix in TOOLING_CORE)
        and top_level(edge.target) in {"agent", "presentation", "config", "mcp", "lsp"}
    ]
    assert violations == [], "tooling core reverse dependencies:\n" + format_edges(violations)


def test_mcp_core_does_not_import_tooling_llm_config_agent_or_settings() -> None:
    violations = [
        edge
        for edge in import_edges()
        if is_under(edge.source, "voidx.mcp")
        and top_level(edge.target) in {"tooling", "llm", "config", "agent"}
    ]
    assert violations == [], "mcp core reverse dependencies:\n" + format_edges(violations)
    manager = (PACKAGE_ROOT / "mcp/application/manager.py").read_text(encoding="utf-8")
    for forbidden in ("Settings", "registry", "permission"):
        assert forbidden not in manager


def test_lsp_final_layers_exist_and_legacy_modules_are_absent() -> None:
    required = {
        "lsp/domain/__init__.py",
        "lsp/ports/client.py",
        "lsp/ports/operations.py",
        "lsp/application/manager.py",
        "lsp/application/service.py",
        "lsp/adapters/client/__init__.py",
    }
    missing = sorted(path for path in required if not (PACKAGE_ROOT / path).is_file())
    legacy = sorted(
        path
        for path in ("lsp/client.py", "lsp/manager.py", "lsp/schema.py", "lsp/service.py")
        if (PACKAGE_ROOT / path).exists()
    )
    assert missing == []
    assert legacy == []


def test_lsp_dependency_direction_and_tooling_adapter_boundary() -> None:
    manager_imports = _module_imports("voidx.lsp.application.manager")
    assert any(target.startswith("voidx.lsp.ports.client") for target in manager_imports)
    assert not any(target.startswith("voidx.lsp.adapters") for target in manager_imports)
    service_imports = _module_imports("voidx.lsp.application.service")
    assert any(target.startswith("voidx.lsp.application.manager") for target in service_imports)
    adapter_imports = _module_imports("voidx.tooling.adapters.lsp")
    forbidden = [
        target
        for target in adapter_imports
        if target.startswith(("voidx.lsp.application", "voidx.lsp.adapters"))
    ]
    allowed_lsp = [target for target in adapter_imports if target.startswith("voidx.lsp")]
    assert forbidden == []
    assert all(target.startswith(("voidx.lsp.domain", "voidx.lsp.ports.operations")) for target in allowed_lsp)


def test_mcp_final_layers_exist_and_legacy_manager_and_client_are_absent() -> None:
    assert (PACKAGE_ROOT / "mcp/application/manager.py").is_file()
    assert (PACKAGE_ROOT / "mcp/adapters/client/__init__.py").is_file()
    assert not (PACKAGE_ROOT / "mcp/manager.py").exists()
    assert not (PACKAGE_ROOT / "mcp/client").exists()


def test_tool_execution_context_contains_values_not_service_objects() -> None:
    path = PACKAGE_ROOT / "tooling/domain/context.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    text = ast.unparse(tree)
    context = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ToolExecutionContext")
    forbidden = (
        "ToolRuntime",
        "AgentToolContext",
        "AuthorizationCapabilities",
        "FileTrackingContext",
        "ToolExecutionCapabilities",
        "_RUNTIME_ALIASES",
        "__getattr__",
        "setattr",
        "Callable",
        "manager",
        "controller",
        "registry",
        "gateway",
        "capabilities",
    )
    assert "Any" not in {node.id for node in ast.walk(context) if isinstance(node, ast.Name)}
    assert not any(token.lower() in text.lower() for token in forbidden)
    assert not any(isinstance(node, ast.FunctionDef) and node.name == "__init__" for node in context.body)
    forbidden_fields = {
        "task_intent",
        "goal_type",
        "goal_target",
        "active_workflow_names",
        "workflow_runs",
        "workflow_route",
        "goal_phase",
        "loop_phase",
        "format_after_edit_enabled",
    }
    declared_fields = {
        node.target.id
        for node in context.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert declared_fields.isdisjoint(forbidden_fields)
    risk_text = (PACKAGE_ROOT / "tooling/domain/risk.py").read_text(encoding="utf-8")
    approved = ast.parse(risk_text)
    risk_class = next(
        node
        for node in approved.body
        if isinstance(node, ast.ClassDef) and node.name == "ApprovedToolRisk"
    )
    assert "ConfigDict(frozen=True)" in ast.unparse(risk_class)


def test_lsp_post_edit_has_no_optional_context_capability_lookup() -> None:
    text = (PACKAGE_ROOT / "tooling/adapters/lsp_post_edit.py").read_text(encoding="utf-8")
    assert 'getattr(ctx, "format_after_edit_enabled"' not in text
    assert 'getattr(ctx, "post_edit_formatter"' not in text
    lsp_text = (PACKAGE_ROOT / "tooling/adapters/lsp.py").read_text(encoding="utf-8")
    assert 'getattr(ctx, "authorization_service"' not in lsp_text


def test_tool_execution_context_is_frozen_and_uses_immutable_containers() -> None:
    path = PACKAGE_ROOT / "tooling/domain/context.py"
    text = path.read_text(encoding="utf-8")
    assert "ConfigDict(frozen=True)" in text
    assert "list[" not in text


def test_mcp_gateway_depends_on_tooling_port_not_concrete_manager() -> None:
    path = PACKAGE_ROOT / "tooling/adapters/mcp.py"
    text = path.read_text(encoding="utf-8")
    assert "voidx.tooling.ports.mcp" in text
    assert "McpGateway" in text
    assert "voidx.mcp.application.manager" not in text
    assert "McpManager" not in text


def test_p3_narrow_runtime_ports_and_agent_context_exist() -> None:
    required = (
        "tooling/ports/invoker.py",
        "tooling/ports/process.py",
        "tooling/ports/mcp.py",
        "agent/adapters/tools/context.py",
    )
    assert all((PACKAGE_ROOT / path).is_file() for path in required)


def test_three_plugin_factories_are_composed_only_in_bootstrap_tooling() -> None:
    bootstrap = (PACKAGE_ROOT / "bootstrap/tooling.py").read_text(encoding="utf-8")
    for name in ("build_builtin_plugins", "build_integration_plugins", "build_agent_plugins"):
        definitions = _function_definitions(name)
        assert len(definitions) == 1
        assert name in bootstrap
    wiring = (PACKAGE_ROOT / "agent/adapters/langgraph/runtime/wiring.py").read_text(encoding="utf-8")
    assert not any(name in wiring for name in ("build_builtin_plugins", "build_integration_plugins", "build_agent_plugins"))


def test_mcp_gateway_is_part_of_frozen_tool_catalog() -> None:
    bootstrap = (PACKAGE_ROOT / "bootstrap/tooling.py").read_text(encoding="utf-8")
    assert '"mcp"' in bootstrap
    assert "McpGatewayTool(None)" in bootstrap
    assert "tools.replace(gateway_tool.id" in bootstrap
    assert "tools.register_plugin(gateway_tool)" not in bootstrap


def test_registry_rejects_duplicate_ids_on_every_registration_path() -> None:
    from voidx.tooling.application.registry import ToolRegistry

    class Plugin:
        id = "same"
        description = "same"

        def parameters_schema(self) -> dict:
            return {}

        async def execute(self, args, ctx):  # pragma: no cover - registration only
            raise NotImplementedError

    from voidx.tooling.domain.capability import ToolCapability

    registry = ToolRegistry(
        [Plugin()], capabilities={"same": ToolCapability.ORCHESTRATION}
    )
    with pytest.raises(ValueError, match="Duplicate tool id: same"):
        registry.register_plugin(Plugin(), capability=ToolCapability.ORCHESTRATION)
    with pytest.raises(ValueError, match="Duplicate tool id: same"):
        registry.register(
            "same", Plugin(), "same", {}, capability=ToolCapability.ORCHESTRATION
        )


def test_policy_git_does_not_import_builtin_or_private_implementation() -> None:
    violations = [
        edge
        for edge in import_edges()
        if is_under(edge.source, "voidx.tooling.policy.git")
        and is_under(edge.target, "voidx.tooling.builtin")
    ]
    assert violations == [], "policy/git imports builtin:\n" + format_edges(violations)


def test_p3_debt_is_fully_removed() -> None:
    debt_path = ROOT / "src/tests/fixtures/architecture/current_edges.json"
    debt = json.loads(debt_path.read_text(encoding="utf-8"))
    p3_debt = [item for item in debt if item.get("remove_by") == "P3"]
    assert p3_debt == []


_P3_MANIFESTS = (
    ("tools", "### 10.4.1", "### 10.4.2", "src/voidx/tools"),
    ("permission", "### 10.4.3", "### 10.5", "src/voidx/permission"),
)


def _manifest_rows(section: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        cells = line.split("|")
        if len(cells) < 4:
            continue
        source = cells[1].strip().strip("`")
        if source.startswith("src/voidx/") and source.endswith(".py"):
            rows.append((source, cells[2].strip()))
    return rows


def _expand_manifest_path(path: str) -> tuple[str, ...]:
    match = __import__("re").search(r"\{([^{}]+)\}", path)
    if match is None:
        return (path,)
    return tuple(
        path[:match.start()] + option + path[match.end():]
        for option in match.group(1).split(",")
    )


def _manifest_targets(disposition: str) -> set[str]:
    targets: set[str] = set()
    for token in __import__("re").findall(r"`([^`]+)`", disposition):
        path = token.split(":", 1)[0]
        if not path.startswith(("src/voidx/", "tooling/", "agent/", "platform/", "lsp/", "mcp/", "config/")):
            continue
        if not (path.endswith(".py") or path.endswith("/")):
            continue
        normalized = path if path.startswith("src/") else f"src/voidx/{path}"
        targets.update(_expand_manifest_path(normalized))
    return targets


def test_p3_authoritative_manifests_are_complete_and_landed() -> None:
    spec = (ROOT / "docs/archive/src-voidx-modular-architecture-refactor-2026-08-05.md").read_text(encoding="utf-8")
    fixture = json.loads(
        (ROOT / "src/tests/fixtures/architecture/p3_pre_migration_sources.json").read_text(encoding="utf-8")
    )
    missing_targets: set[str] = set()
    remaining_sources: set[str] = set()

    for name, start, end, legacy_root in _P3_MANIFESTS:
        section = spec.split(start, 1)[1].split(end, 1)[0]
        rows = _manifest_rows(section)
        sources = [source for source, _ in rows]
        assert len(sources) == len(set(sources)), f"duplicate {name} manifest source"
        assert set(sources) == set(fixture[name]), f"{name} manifest source set drifted"
        assert all("待定" not in disposition and "TBD" not in disposition for _, disposition in rows)

        for source, disposition in rows:
            source_path = ROOT / source
            if source_path.exists():
                remaining_sources.add(source)
            targets = _manifest_targets(disposition)
            if not targets:
                assert "删除" in disposition, f"no target or delete disposition for {source}"
            missing_targets.update(target for target in targets if not (ROOT / target).exists())

        assert not (ROOT / legacy_root).exists(), f"legacy package remains: {legacy_root}"

    assert remaining_sources == set(), f"legacy manifest sources remain: {sorted(remaining_sources)}"
    assert missing_targets == set(), f"manifest targets missing: {sorted(missing_targets)}"


def test_tool_plugin_contract_has_no_abc_compatibility_layer() -> None:
    tool_port = (PACKAGE_ROOT / "tooling/ports/tool.py").read_text(encoding="utf-8")
    assert "BaseTool" not in tool_port
    assert "ABC" not in tool_port
    assert "abstractmethod" not in tool_port
    assert _function_definitions("parameters_schema")


def test_permission_state_is_owned_by_in_memory_adapter() -> None:
    service = (PACKAGE_ROOT / "tooling/application/permission_service.py").read_text(encoding="utf-8")
    port = (PACKAGE_ROOT / "tooling/ports/permission_state.py").read_text(encoding="utf-8")
    state = (PACKAGE_ROOT / "tooling/adapters/permission/in_memory_state.py").read_text(encoding="utf-8")
    for forbidden in (
        "self._session_allow",
        "self._session_deny",
        "self._runtime_grants",
        "self._session_grants",
        "self._persistent_grants",
        "self.revocation_epoch +=",
        "self.state_revision +=",
        "self.permissions_revision +=",
    ):
        assert forbidden not in service
    for mutable_container in (
        "runtime_grants: list",
        "session_grants: list",
        "persistent_grants: list",
        "active_execution_leases: set",
    ):
        assert mutable_container not in port
    for owner in (
        "self.runtime_grants",
        "self.session_grants",
        "self.persistent_grants",
        "self.revocation_epoch",
        "self.active_execution_leases",
    ):
        assert owner in state


def test_lsp_tooling_uses_complete_operations_service() -> None:
    bootstrap = (PACKAGE_ROOT / "bootstrap/tooling.py").read_text(encoding="utf-8")
    adapter = (PACKAGE_ROOT / "tooling/adapters/lsp.py").read_text(encoding="utf-8")
    service = (PACKAGE_ROOT / "lsp/application/service.py").read_text(encoding="utf-8")
    assert "lsp_operations = LspOperationsService(lsp_manager)" in bootstrap
    assert "LspTool(lsp_operations" in bootstrap
    assert "LspFormatTool(lsp_operations)" in bootstrap
    assert "getattr(" not in adapter
    assert "class LspOperationsService" in service
    assert "async def format_range" in service


def test_lsp_post_edit_uses_operations_service_without_manager_fallback() -> None:
    post_edit = (PACKAGE_ROOT / "tooling/adapters/lsp_post_edit.py").read_text(encoding="utf-8")
    executor = (PACKAGE_ROOT / "agent/adapters/langgraph/runtime/tool_executor/executor.py").read_text(encoding="utf-8")
    subagent = (PACKAGE_ROOT / "agent/adapters/langgraph/runtime/subagent.py").read_text(encoding="utf-8")
    assert "getattr(" not in post_edit
    assert "formatted_range_text" not in post_edit
    assert "host._lsp_operations" in executor
    assert "LspOperationsService(lsp_manager)" in subagent
