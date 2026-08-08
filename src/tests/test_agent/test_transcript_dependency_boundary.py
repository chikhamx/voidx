"""Transcript snapshots remain owned by presentation."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_agent_transcript_runtime_does_not_import_presentation():
    paths = (
        ROOT / "src/voidx/agent/adapters/persistence/session_repository.py",
        ROOT / "src/voidx/agent/adapters/langgraph/runtime/session_runtime.py",
        ROOT / "src/voidx/agent/adapters/langgraph/execution.py",
    )
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in paths
        if any(module == "voidx.presentation" or module.startswith("voidx.presentation.") for module in _imports(path))
    ]

    assert offenders == []
