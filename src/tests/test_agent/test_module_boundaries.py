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

    assert "voidx.memory.model_profiles" not in imports


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


def test_llm_instruction_uses_workflow_types_boundary():
    imports = _imported_modules("src/voidx/llm/instruction.py")

    assert "voidx.workflow.runtime" not in imports
    assert "voidx.workflow.types" in imports


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


def test_agent_graph_uses_runtime_ui_boundary():
    offenders = []
    for path in _python_files("src/voidx/agent/graph"):
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
        "voidx.agent.attachments",
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
