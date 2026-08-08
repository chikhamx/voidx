import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PRESENTATION_ROOT = ROOT / "src/voidx/presentation"
FORBIDDEN_PACKAGES = (
    "voidx.agent.application",
    "voidx.agent.adapters.langgraph",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_presentation_does_not_depend_on_agent_application_or_langgraph_adapter():
    violations = {
        path.relative_to(ROOT).as_posix(): sorted(
            module
            for module in _imports(path)
            if module in FORBIDDEN_PACKAGES
            or module.startswith(tuple(f"{package}." for package in FORBIDDEN_PACKAGES))
        )
        for path in sorted(PRESENTATION_ROOT.rglob("*.py"))
    }

    assert {path: imports for path, imports in violations.items() if imports} == {}
