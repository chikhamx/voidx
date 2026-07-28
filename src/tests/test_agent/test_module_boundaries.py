import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _imported_modules(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imported_names(path: str, module: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


def _python_files(path: str) -> list[Path]:
    return sorted((ROOT / path).glob("*.py"))


def _python_files_recursive(path: str) -> list[Path]:
    return sorted((ROOT / path).rglob("*.py"))


def test_config_settings_uses_memory_service_boundary():
    imports = _imported_modules("src/voidx/config/settings.py")

    assert not {module for module in imports if module.startswith("voidx.memory")}


def test_workflow_service_does_not_import_skills_schema():
    imports = _imported_modules("src/voidx/workflow/service.py")

    assert "voidx.skills.schema" not in imports


def test_bash_tool_uses_permission_service_boundary():
    imports = _imported_modules("src/voidx/tools/bash/safety.py")

    assert "voidx.permission.engine" not in imports
    assert "voidx.permission.sandbox" not in imports


def test_workflow_runtime_types_have_public_types_boundary():
    runtime_imports = _imported_modules("src/voidx/workflow/runtime.py")

    assert "voidx.workflow.types" in runtime_imports


def test_workflow_types_has_no_workflow_implementation_imports():
    imports = _imported_modules("src/voidx/workflow/types.py")

    assert "voidx.workflow.context" not in imports
    assert "voidx.workflow.policy" not in imports


def test_agent_instruction_uses_workflow_types_boundary():
    imports = _imported_modules("src/voidx/agent/application/instruction.py")

    assert "voidx.workflow.runtime" not in imports
    assert "voidx.workflow.types" in imports


def test_llm_layer_does_not_import_agent_runtime_concerns():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/llm")
        if _imported_modules(path.relative_to(ROOT).as_posix())
        & {"voidx.agent", "voidx.mcp.auto", "voidx.skills.registry", "voidx.skills.service", "voidx.workflow.service"}
    ]

    assert offenders == []


def test_memory_service_does_not_import_private_session_now():
    imports = _imported_names("src/voidx/memory/service.py", "voidx.memory.session")

    assert "_now" not in imports


def test_cross_module_workflow_type_consumers_use_public_boundaries():
    internal_imports = {
        "voidx.workflow.policy",
        "voidx.workflow.runtime",
    }
    files = [
        "src/voidx/memory/runtime_state.py",
        "src/voidx/runtime/task_state.py",
        "src/voidx/tools/workflow.py",
        "src/voidx/tools/base.py",
        "src/voidx/workflow/auto_advance.py",
    ]
    offenders = [
        path
        for path in files
        if _imported_modules(path) & internal_imports
    ]

    assert offenders == []


def test_agent_graph_uses_llm_service_boundary():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files("src/voidx/agent/graph")
        if "voidx.llm.provider" in _imported_modules(path.relative_to(ROOT).as_posix())
    ]

    assert offenders == []


def test_agent_module_uses_llm_service_boundary():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent")
        if "voidx.llm.provider" in _imported_modules(path.relative_to(ROOT).as_posix())
    ]

    assert offenders == []


def test_agent_module_uses_workflow_public_boundaries():
    internal_workflow_modules = {
        "voidx.workflow.auto_advance",
        "voidx.workflow.context",
        "voidx.workflow.policy",
        "voidx.workflow.runtime",
    }
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent")
        if _imported_modules(path.relative_to(ROOT).as_posix()) & internal_workflow_modules
    ]

    assert offenders == []


def test_agent_module_uses_memory_service_boundary():
    internal_memory_modules = {
        "voidx.memory.context_frames",
        "voidx.memory.model_profiles",
        "voidx.memory.runtime_state",
        "voidx.memory.session",
        "voidx.memory.transcript",
    }
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent")
        if _imported_modules(path.relative_to(ROOT).as_posix()) & internal_memory_modules
    ]

    assert offenders == []


def test_agent_module_uses_permission_service_boundary():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent")
        if "voidx.permission.engine" in _imported_modules(path.relative_to(ROOT).as_posix())
    ]

    assert offenders == []


def test_langgraph_infrastructure_uses_runtime_ui_boundary():
    offenders = []
    for path in _python_files_recursive("src/voidx/agent/infrastructure/langgraph"):
        rel = path.relative_to(ROOT).as_posix()
        direct_ui_imports = {
            module
            for module in _imported_modules(rel)
            if module.startswith("voidx.ui.")
        }
        if direct_ui_imports:
            offenders.append(rel)

    assert offenders == []


def test_agent_module_uses_runtime_ui_boundary():
    offenders = []
    for path in _python_files_recursive("src/voidx/agent"):
        rel = path.relative_to(ROOT).as_posix()
        direct_ui_imports = {
            module
            for module in _imported_modules(rel)
            if module.startswith("voidx.ui.")
        }
        if direct_ui_imports:
            offenders.append(rel)

    assert offenders == []


def test_agent_module_uses_tools_service_boundary():
    internal_tools_modules = {
        "voidx.tools.agent",
        "voidx.tools.base",
        "voidx.tools.registry",
        "voidx.tools.task_tracker",
    }
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent")
        if _imported_modules(path.relative_to(ROOT).as_posix()) & internal_tools_modules
    ]

    assert offenders == []


def test_agent_module_uses_skills_service_boundary():
    internal_skills_modules = {
        "voidx.skills.context",
        "voidx.skills.references",
        "voidx.skills.registry",
    }
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent")
        if _imported_modules(path.relative_to(ROOT).as_posix()) & internal_skills_modules
    ]

    assert offenders == []


def test_ui_uses_existing_service_boundaries():
    internal_modules = {
        "voidx.agent.application.attachments",
        "voidx.memory.transcript",
        "voidx.skills.registry",
        "voidx.tools.base",
        "voidx.tools.todo",
    }
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/ui")
        if _imported_modules(path.relative_to(ROOT).as_posix()) & internal_modules
    ]

    assert offenders == []


def test_external_modules_do_not_import_agent_graph():
    external_roots = ("ui", "tools", "runtime", "memory", "workflow")
    offenders: list[str] = []
    for root in external_roots:
        for path in _python_files_recursive(f"src/voidx/{root}"):
            rel = path.relative_to(ROOT).as_posix()
            imports = _imported_modules(rel)
            if any(module == "voidx.agent.infrastructure.langgraph.runtime" or module.startswith("voidx.agent.infrastructure.langgraph.runtime.") for module in imports):
                offenders.append(rel)

    assert offenders == []


def test_tools_do_not_import_agent():
    offenders: list[str] = []
    for path in _python_files_recursive("src/voidx/tools"):
        rel = path.relative_to(ROOT).as_posix()
        if any(module == "voidx.agent" or module.startswith("voidx.agent.") for module in _imported_modules(rel)):
            offenders.append(rel)

    assert offenders == []


def test_agent_application_does_not_import_langgraph():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files_recursive("src/voidx/agent/application")
        if any(module == "langgraph" or module.startswith("langgraph.") for module in _imported_modules(path.relative_to(ROOT).as_posix()))
    ]

    assert offenders == []




def test_agent_production_entrypoints_do_not_import_legacy_graph():
    entrypoints = (
        "src/voidx/agent/composition.py",
        "src/voidx/agent/facade.py",
        "src/voidx/main.py",
    )
    offenders = [
        path
        for path in entrypoints
        if any(
            module == "voidx.agent.infrastructure.langgraph.runtime" or module.startswith("voidx.agent.infrastructure.langgraph.runtime.")
            for module in _imported_modules(path)
        )
    ]

    assert offenders == []



def test_run_loop_lives_in_application_without_legacy_alias():
    application_service = ROOT / "src/voidx/agent/application/agent_service.py"
    legacy_run_loop = ROOT / "src/voidx/agent/graph/run_loop.py"

    assert application_service.is_file()
    assert not legacy_run_loop.exists()

    source = application_service.read_text(encoding="utf-8")
    assert "class AgentService" in source
    assert "GraphRunLoopMixin" not in source
    assert not any(
        module == "voidx.agent.infrastructure.langgraph.runtime" or module.startswith("voidx.agent.infrastructure.langgraph.runtime.")
        for module in _imported_modules("src/voidx/agent/application/agent_service.py")
    )



def test_legacy_graph_and_contracts_are_deleted():
    assert not (ROOT / "src/voidx/agent/graph").exists()
    assert not (ROOT / "src/voidx/agent/task_state.py").exists()
    assert not (ROOT / "src/voidx/agent/slash/host.py").exists()


def test_agent_has_no_mixin_or_compatibility_adapter_modules():
    agent_root = ROOT / "src/voidx/agent"
    forbidden_files = {
        "title_operations.py",
        "session_operations.py",
        "transcript_operations.py",
        "turn_operations.py",
        "tool_execution.py",
        "permissions.py",
        "llm.py",
        "code_ide.py",
        "guide.py",
        "init.py",
        "lsp.py",
        "mcp.py",
        "model.py",
        "profile.py",
        "session.py",
        "skills.py",
        "upgrade.py",
        "host.py",
    }
    forbidden_symbols = (
        "VoidXGraph",
        "SlashHostAdapter",
        "SlashCommandHost",
        "GraphRunLoopMixin",
        "CompactionOperations",
        "ToolExecutionOperations",
    )

    offenders = [
        path.relative_to(agent_root).as_posix()
        for path in agent_root.rglob("*.py")
        if path.name in forbidden_files
        and (
            "infrastructure/langgraph/runtime/" in path.as_posix()
            or "/slash/" in path.as_posix()
        )
        and "/slash/commands/" not in path.as_posix()
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in agent_root.rglob("*.py"))

    assert offenders == []
    assert all(symbol not in source for symbol in forbidden_symbols)
