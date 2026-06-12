import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_config_settings_uses_memory_service_boundary():
    imports = _imported_modules("src/voidx/config/settings.py")

    assert "voidx.memory.model_profiles" not in imports


def test_workflow_service_does_not_import_skills_schema():
    imports = _imported_modules("src/voidx/workflow/service.py")

    assert "voidx.skills.schema" not in imports


def test_bash_tool_uses_permission_service_boundary():
    imports = _imported_modules("src/voidx/tools/bash.py")

    assert "voidx.permission.engine" not in imports
    assert "voidx.permission.sandbox" not in imports
