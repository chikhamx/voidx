"""P4 presentation isolation architecture guards."""

from __future__ import annotations

import ast
from pathlib import Path

from .import_graph import format_edges, import_edges


ROOT = Path(__file__).resolve().parents[3]
AGENT_APPLICATION = ROOT / "src" / "voidx" / "agent" / "application"
PRESENTATION = ROOT / "src" / "voidx" / "presentation"
CORE_PACKAGE_PREFIXES = (
    "voidx.agent",
    "voidx.tooling",
    "voidx.mcp",
    "voidx.llm",
    "voidx.lsp",
    "voidx.skills",
)


def _python_files(root: Path):
    return (path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_core_packages_do_not_import_presentation_or_legacy_runtime_ui():
    violations = [
        edge
        for edge in import_edges()
        if edge.source.startswith(CORE_PACKAGE_PREFIXES)
        and (
            edge.target == "voidx.presentation"
            or edge.target.startswith("voidx.presentation.")
            or edge.target == "voidx.runtime.ui"
            or edge.target.startswith("voidx.runtime.ui.")
            or edge.target == "voidx.runtime.ui_port"
            or edge.target.startswith("voidx.runtime.ui_port.")
        )
    ]

    assert violations == [], "core presentation/runtime UI dependencies:\n" + format_edges(violations)


def test_presentation_does_not_receive_execution_host_bag():
    offenders: list[str] = []
    for path in _python_files(PRESENTATION):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if argument.arg == "execution":
                    offenders.append(f"{path.relative_to(ROOT)}:{argument.lineno}")

    assert offenders == [], "presentation receives the broad execution host:\n" + "\n".join(offenders)


def test_presentation_does_not_read_collaborator_private_state():
    offenders: list[str] = []
    for path in _python_files(PRESENTATION):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
                continue
            value = node.value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                if value.value.id == "self" and value.attr in {"_execution", "_service"}:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{value.attr}.{node.attr}")
            elif isinstance(value, ast.Name) and value.id in {"execution", "service", "gs", "gateway_session"}:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{value.id}.{node.attr}")

    assert offenders == [], "presentation reads collaborator private state:\n" + "\n".join(offenders)


def test_legacy_runtime_ui_modules_are_removed():
    assert not (ROOT / "src" / "voidx" / "runtime" / "ui.py").exists()
    assert not (ROOT / "src" / "voidx" / "runtime" / "ui_port.py").exists()


def test_agent_service_does_not_depend_on_broad_execution_host():
    path = AGENT_APPLICATION / "agent_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "voidx.agent.ports.execution_host":
            offenders.append(f"import:{node.lineno}")
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "_execution"
                for target in node.targets
            )
        ):
            offenders.append(f"state:{node.lineno}")
    assert offenders == [], "AgentService still depends on broad ExecutionHost:\n" + "\n".join(offenders)


def test_agent_service_constructor_accepts_only_narrow_input_ports():
    path = AGENT_APPLICATION / "agent_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    service = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentService"
    )
    constructor = next(
        node
        for node in service.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )

    arguments = [argument.arg for argument in constructor.args.args[1:]]
    assert arguments == [
        "status_reader",
        "slash_dispatcher",
        "autonomous_router",
        "guidance",
    ]


def test_presentation_entrypoints_do_not_accept_any_collaborators():
    entrypoints = [
        PRESENTATION / "terminal" / "run_loop.py",
        PRESENTATION / "terminal" / "startup.py",
        PRESENTATION / "gateway" / "command_handler.py",
        PRESENTATION / "gateway" / "session_adapter.py",
    ]
    offenders: list[str] = []
    for path in entrypoints:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                annotation = argument.annotation
                if isinstance(annotation, ast.Name) and annotation.id == "Any":
                    offenders.append(f"{path.relative_to(ROOT)}:{argument.lineno}:{argument.arg}")
    assert offenders == [], "presentation entrypoints accept Any collaborators:\n" + "\n".join(offenders)


def test_gateway_command_handler_uses_thread_registry_port():
    path = PRESENTATION / "gateway" / "command_handler.py"
    source = path.read_text(encoding="utf-8")
    assert "_gateway_session" not in source
    assert "Callable[[], Any" not in source
    assert "GatewayThreadRegistry" in source


def test_presentation_runtime_lazy_aggregate_is_removed():
    path = PRESENTATION / "runtime.py"
    assert not path.exists(), "presentation/runtime.py is a renamed lazy UI aggregate"


def test_global_runtime_ui_port_is_removed():
    path = PRESENTATION / "runtime_port.py"
    if not path.exists():
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            any(isinstance(target, ast.Name) and target.id == "runtime_ui_port" for target in node.targets)
            if isinstance(node, ast.Assign)
            else isinstance(node.target, ast.Name) and node.target.id == "runtime_ui_port"
        )
    ]
    assert offenders == [], f"global runtime_ui_port remains at lines {offenders}"


def test_production_agent_composition_requires_explicit_ui_port():
    path = ROOT / "src" / "voidx" / "bootstrap" / "agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_agent_components"
    )
    keyword_defaults = dict(zip((arg.arg for arg in function.args.kwonlyargs), function.args.kw_defaults))
    assert "ui" in keyword_defaults
    assert keyword_defaults["ui"] is None
    source = path.read_text(encoding="utf-8")
    assert "ui or NullAgentUiPort()" not in source


def test_langgraph_execution_requires_explicit_ui_adapter():
    path = ROOT / "src" / "voidx" / "agent" / "infrastructure" / "langgraph" / "execution.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    execution = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LangGraphExecution")
    constructor = next(node for node in execution.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    defaults = dict(zip((arg.arg for arg in constructor.args.kwonlyargs), constructor.args.kw_defaults))
    assert defaults["ui"] is None
    assert "ui or NullAgentUiPort()" not in path.read_text(encoding="utf-8")


def test_agent_service_has_only_four_port_dependencies():
    path = AGENT_APPLICATION / "agent_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentService")
    constructor = next(node for node in service.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    all_arguments = [
        *(argument.arg for argument in constructor.args.args[1:]),
        *(argument.arg for argument in constructor.args.kwonlyargs),
    ]
    assert all_arguments == ["status_reader", "slash_dispatcher", "autonomous_router", "guidance"]
    assigned = {
        target.attr
        for node in ast.walk(constructor)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
    }
    assert assigned <= {"_status_reader", "_slash_dispatcher", "_autonomous_router", "_guidance"}


def test_langgraph_execution_does_not_own_frontend_or_gateway_session():
    path = ROOT / "src" / "voidx" / "agent" / "infrastructure" / "langgraph" / "execution.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in {"_app", "_gateway_session"}
    }
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert forbidden == set()
    assert {"app", "gateway_session"}.isdisjoint(methods)


def test_tool_execution_does_not_read_gateway_private_state():
    root = ROOT / "src" / "voidx" / "agent" / "infrastructure" / "langgraph" / "runtime"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    assert 'getattr(host, "_gateway_session"' not in source
    assert 'getattr(gateway_session, "_run_manager"' not in source




def test_interactive_input_port_does_not_accept_frontend_app():
    path = ROOT / "src" / "voidx" / "agent" / "ports" / "presentation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    protocol = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "InteractiveInputPort")
    dispatch = next(node for node in protocol.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_input")
    arguments = [argument.arg for argument in dispatch.args.args[1:]]
    assert arguments == ["user_input"]


def test_gateway_workspace_lock_is_exposed_through_adapter():
    path = PRESENTATION / "gateway" / "session" / "core.py"
    source = path.read_text(encoding="utf-8")
    assert "GatewayWorkspaceWriteLock" in source
    assert "return self._run_manager" not in source


def test_transcript_persistence_has_one_presentation_adapter_owner():
    assert not (PRESENTATION / "transcript_snapshot.py").exists()
    assert not (PRESENTATION / "transcript_adapter.py").exists()
    assert (PRESENTATION / "adapters" / "persistence" / "transcript_snapshot.py").exists()
    assert (PRESENTATION / "adapters" / "persistence" / "transcript_adapter.py").exists()


def test_presentation_ui_adapter_uses_explicit_frontend_port():
    path = PRESENTATION / "runtime_port.py"
    source = path.read_text(encoding="utf-8")
    assert "getattr(self._frontend" not in source
    assert "Any | None" not in source
    assert "FrontendStatusPort" in source
    assert "FrontendInteractionPort" in source


def test_agent_service_has_no_frontend_binding_entrypoint():
    path = AGENT_APPLICATION / "agent_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentService")
    methods = {
        node.name
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "bind_frontend" not in methods


def test_presentation_protocol_does_not_import_persistence_adapters():
    protocol = PRESENTATION / "protocol"
    violations = [
        edge
        for edge in import_edges()
        if edge.source.startswith("voidx.presentation.protocol")
        and edge.target.startswith("voidx.presentation.adapters")
    ]
    assert violations == [], "protocol imports persistence adapters:\n" + format_edges(violations)


def test_input_adapter_uses_explicit_frontend_protocol():
    path = ROOT / "src" / "voidx" / "agent" / "infrastructure" / "input_adapter.py"
    source = path.read_text(encoding="utf-8")
    assert 'getattr(self._frontend, "hide_command_output"' not in source
