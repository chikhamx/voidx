import ast
from pathlib import Path



AGENT_ROOT = Path(__file__).resolve().parents[3] / "voidx" / "agent"
DOMAIN_ROOT = AGENT_ROOT / "domain"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_agent_internal_modules_import_runtime_task_state_directly() -> None:
    offenders = [
        path.relative_to(AGENT_ROOT).as_posix()
        for path in AGENT_ROOT.rglob("*.py")
        if "voidx.agent.task_state" in _imports(path)
    ]

    assert offenders == []




def test_domain_has_no_infrastructure_dependencies() -> None:
    forbidden_prefixes = (
        "voidx.agent.infrastructure.langgraph.runtime",
        "voidx.config",
        "voidx.memory",
        "voidx.permission",
        "voidx.tools",
        "voidx.ui",
        "voidx.workflow",
        "langgraph",
    )
    offenders = {
        path.name: sorted(
            module
            for module in _imports(path)
            if module.startswith(forbidden_prefixes)
        )
        for path in DOMAIN_ROOT.glob("*.py")
    }

    assert {path: imports for path, imports in offenders.items() if imports} == {}
