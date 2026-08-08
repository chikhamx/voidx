import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APPLICATION_ROOT = ROOT / "src/voidx/agent/application"
FORBIDDEN_PACKAGES = ("voidx.llm", "voidx.mcp", "voidx.skills", "voidx.tooling")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_agent_application_depends_on_ports_not_external_business_packages():
    violations = {
        path.relative_to(ROOT).as_posix(): sorted(
            module
            for module in _imports(path)
            if module in FORBIDDEN_PACKAGES
            or module.startswith(tuple(f"{package}." for package in FORBIDDEN_PACKAGES))
        )
        for path in sorted(APPLICATION_ROOT.rglob("*.py"))
    }

    assert {path: imports for path, imports in violations.items() if imports} == {}
