import ast
from pathlib import Path



AGENT_ROOT = Path(__file__).resolve().parents[2] / "voidx" / "agent"
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
        "voidx.presentation",
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



def _attribute_calls(path: Path, attr: str) -> int:
    """Count ``x.<attr>(...)`` call sites in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        func = node.func if isinstance(node, ast.Call) else None
        if isinstance(func, ast.Attribute) and func.attr == attr:
            count += 1
    return count




def test_only_facade_and_engine_adapter_call_run_turn() -> None:
    """``run_turn`` may only be invoked on the runtime facade or the execution.

    Allowed call sites:
    - ``AgentRuntime.run_turn`` delegating to ``turn_engine.run`` (facade internal).
    - application services calling ``self._runtime.run_turn``.
    - ``LangGraphTurnEngine`` calling ``self._execution.run_turn``.
    - runtime-backed autonomous dispatchers/schedulers invoking injected runners.
    """
    allowed_files = {
        "runtime/runtime.py",
        "application/agent_service.py",
        "application/chat_service.py",
        "application/coding_service.py",
        "infrastructure/langgraph/adapter.py",
        "application/runtime/dispatcher.py",
        "application/automation/loop/scheduler.py",
        "application/automation/goal/runner.py",
        "application/automation/goal/goal_idle.py",
        "application/automation/loop/loop_idle.py",
        "infrastructure/langgraph/execution.py",
    }
    offenders = []
    for path in AGENT_ROOT.rglob("*.py"):
        rel = path.relative_to(AGENT_ROOT).as_posix()
        if rel in allowed_files:
            continue
        if _attribute_calls(path, "run_turn") > 0:
            offenders.append(rel)
    assert offenders == []


def test_codebase_does_not_call_synthetic_turn() -> None:
    offenders = []
    for path in AGENT_ROOT.rglob("*.py"):
        rel = path.relative_to(AGENT_ROOT).as_posix()
        if _attribute_calls(path, "run_synthetic_turn") > 0:
            offenders.append(rel)
    assert offenders == []
