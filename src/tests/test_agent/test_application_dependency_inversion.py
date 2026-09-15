import ast
from pathlib import Path

import pytest

from ..dependency_policy import AGENT_APPLICATION_DTO_DEPENDENCIES

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
            if (
                module in FORBIDDEN_PACKAGES
                or module.startswith(tuple(f"{package}." for package in FORBIDDEN_PACKAGES))
            )
            and (
                ".".join(path.relative_to(ROOT / "src").with_suffix("").parts),
                module,
            ) not in AGENT_APPLICATION_DTO_DEPENDENCIES
        )
        for path in sorted(APPLICATION_ROOT.rglob("*.py"))
    }

    assert {path: imports for path, imports in violations.items() if imports} == {}


@pytest.mark.parametrize("import_style", ["import", "from"])
@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        ("runtime.interaction_coordinator", "voidx.tooling.domain.interaction", True),
        ("runtime.run_supervisor", "voidx.tooling.domain.interaction", False),
        ("chat_service", "voidx.tooling.domain.interaction", False),
        ("runtime.interaction_coordinator.nested", "voidx.tooling.domain.interaction", False),
        ("runtime.interaction_coordinator_extra", "voidx.tooling.domain.interaction", False),
        ("runtime.interaction_coordinator", "voidx.tooling.domain.interaction.nested", False),
        ("runtime.interaction_coordinator", "voidx.tooling.domain.interaction_extra", False),
        ("runtime.interaction_coordinator", "voidx.tooling", False),
        ("runtime.interaction_coordinator", "voidx.tooling.domain", False),
        ("runtime.interaction_coordinator", "voidx.tooling.domain.ui_events", False),
        ("runtime.interaction_coordinator", "voidx.tooling.application.permission_service", False),
        ("runtime.interaction_coordinator", "voidx.tooling.adapters.tools", False),
        ("runtime.interaction_coordinator", "voidx.llm.usage", False),
        ("runtime.interaction_coordinator", "voidx.mcp", False),
        ("runtime.interaction_coordinator", "voidx.skills.registry", False),
        ("runtime.interaction_coordinator", "voidx.agent.ports.events", True),
    ],
)
def test_application_dependency_boundary_is_exact(
    tmp_path, monkeypatch, source, target, allowed, import_style
):
    application_root = tmp_path / "src/voidx/agent/application"
    path = application_root / (source.replace(".", "/") + ".py")
    path.parent.mkdir(parents=True)
    statement = f"import {target}" if import_style == "import" else f"from {target} import DTO"
    path.write_text(statement + "\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "APPLICATION_ROOT", application_root)

    if allowed:
        test_agent_application_depends_on_ports_not_external_business_packages()
    else:
        with pytest.raises(AssertionError) as error:
            test_agent_application_depends_on_ports_not_external_business_packages()
        assert target in str(error.value)
        assert path.relative_to(tmp_path).as_posix() in str(error.value)
