from __future__ import annotations

import ast
from pathlib import Path

from .import_graph import format_edges, import_edges, is_under, without_debt

STANDARD_LAYERS = ("domain", "ports", "application", "adapters")
FEATURES = {"agent", "tooling", "llm", "mcp", "lsp", "skills", "config", "presentation"}


def _layer(module: str) -> str | None:
    for part in module.split("."):
        if part in STANDARD_LAYERS:
            return part
    return None


def test_standard_layer_direction():
    order = {"domain": 0, "ports": 1, "application": 2, "adapters": 3}
    violations = []
    for edge in without_debt(import_edges(), "layer_dependency"):
        if is_under(edge.source, "voidx.bootstrap"):
            continue
        source_layer = _layer(edge.source)
        target_layer = _layer(edge.target)
        if source_layer and target_layer and order[source_layer] < order[target_layer]:
            violations.append(edge)
    assert violations == [], "forbidden layer dependencies:\n" + format_edges(violations)


def test_composition_binding_is_bootstrap_only():
    violations = []
    for edge in without_debt(import_edges(), "composition_binding"):
        source_layer = _layer(edge.source)
        if ".adapters" in edge.target and source_layer in {"domain", "ports", "application"} and not (
            is_under(edge.source, "voidx.bootstrap")
            or is_under(edge.source, "voidx.presentation")
        ):
            violations.append(edge)
    assert violations == [], "non-bootstrap adapter binding:\n" + format_edges(violations)


def test_no_cross_feature_concrete_adapter_imports():
    violations = []
    for edge in without_debt(import_edges(), "cross_feature_adapter"):
        source_features = [part for part in edge.source.split(".") if part in FEATURES]
        target_features = [part for part in edge.target.split(".") if part in FEATURES]
        if (
            ".adapters" in edge.source
            and ".adapters" in edge.target
            and source_features
            and target_features
            and source_features[0] != target_features[0]
            and not is_under(edge.source, "voidx.bootstrap")
        ):
            violations.append(edge)
    assert violations == [], "cross-feature adapter dependencies:\n" + format_edges(violations)




def _adapter_subtree(module: str) -> str | None:
    parts = module.split(".")
    if "adapters" not in parts:
        return None
    index = parts.index("adapters")
    if index + 1 >= len(parts):
        return ".".join(parts[: index + 1])
    return ".".join(parts[: index + 2])


def _same_adapter_subtree(source: str, target: str) -> bool:
    source_subtree = _adapter_subtree(source)
    return source_subtree is not None and source_subtree == _adapter_subtree(target)



def test_same_adapter_subtree_allows_internal_helpers() -> None:
    assert _same_adapter_subtree(
        "voidx.agent.adapters.langgraph.execution",
        "voidx.agent.adapters.langgraph.runtime.core.helpers._invalidate_tui",
    )
    assert not _same_adapter_subtree(
        "voidx.agent.adapters.langgraph.execution",
        "voidx.agent.application.runtime._private",
    )

def test_no_cross_layer_private_imports():
    violations: list[str] = []
    root = Path(__file__).resolve().parents[3] / "src" / "voidx"
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        source = "voidx." + path.relative_to(root).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.names:
                private = [alias.name for alias in node.names if alias.name.startswith("_")]
                for name in private:
                    if node.level > 0 and _adapter_subtree(source) is not None:
                        continue
                    target = f"{node.module}.{name}"
                    if _same_adapter_subtree(source, target):
                        continue
                    edge = type("PrivateImport", (), {"source": source, "target": target})
                    if not without_debt([edge], "private_import"):
                        continue
                    violations.append(f"{path}:{node.lineno} -> {name}")
            elif isinstance(node, ast.Import):
                private = [
                    alias.name
                    for alias in node.names
                    if alias.name.rsplit(".", 1)[-1].startswith("_")
                ]
                for name in private:
                    if _same_adapter_subtree(source, name):
                        continue
                    edge = type("PrivateImport", (), {"source": source, "target": name})
                    if not without_debt([edge], "private_import"):
                        continue
                    violations.append(f"{path}:{node.lineno} -> {name}")
    assert violations == [], "private imports:\n" + "\n".join(violations)




def test_legacy_ui_package_is_removed():
    root = Path(__file__).resolve().parents[3]

    assert not (root / "src" / "voidx" / "ui").exists()
    violations = [
        edge
        for edge in import_edges()
        if edge.target == "voidx.ui" or edge.target.startswith("voidx.ui.")
    ]
    assert violations == [], "legacy ui imports:\n" + format_edges(violations)


def test_agent_core_does_not_depend_on_presentation():
    violations = []
    core_prefixes = (
        "voidx.agent.domain",
        "voidx.agent.ports",
        "voidx.agent.application",
    )
    presentation_prefixes = ("voidx.ui", "voidx.presentation")
    for edge in import_edges():
        if edge.source.startswith(core_prefixes) and edge.target.startswith(presentation_prefixes):
            violations.append(edge)
    assert violations == [], "agent core presentation dependencies:\n" + format_edges(violations)


def test_application_does_not_construct_presentation_components():
    root = Path(__file__).resolve().parents[3] / "src" / "voidx" / "agent" / "application"
    concrete_types = {
        "CompositeEventConsumer",
        "DockEventConsumer",
        "GatewayEventConsumer",
        "GatewayHeadlessFrontend",
        "GatewayServer",
        "GatewaySession",
    }
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
            if name in concrete_types or name == "create_frontend":
                offenders.append(f"{path}:{node.lineno}:{name}")
    assert offenders == [], "application presentation composition:\n" + "\n".join(offenders)


def test_no_literal_internal_dynamic_imports():
    violations = [
        edge for edge in without_debt(import_edges(), "dynamic_import") if edge.dynamic
    ]
    assert violations == [], "dynamic internal imports:\n" + format_edges(violations)


def test_package_initializers_have_no_registration_side_effects():
    root = Path(__file__).resolve().parents[3] / "src" / "voidx"
    offenders = []
    for path in root.rglob("__init__.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                "register", "bind", "add_tool", "register_provider"
            }:
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], "package registration side effects:\n" + "\n".join(offenders)


def test_bootstrap_has_no_dynamic_dependency_probing():
    root = Path(__file__).resolve().parents[3] / "src" / "voidx" / "bootstrap"
    offenders = []
    if root.exists():
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                    "getattr", "hasattr"
                }:
                    offenders.append(f"{path}:{node.lineno}:{node.func.id}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SimpleNamespace":
                    offenders.append(f"{path}:{node.lineno}:SimpleNamespace")
    assert offenders == [], "bootstrap dependency probing:\n" + "\n".join(offenders)
