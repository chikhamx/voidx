from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = ROOT / "src" / "voidx"


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_type_checking_guard(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "TYPE_CHECKING"
        or isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
        and node.attr == "TYPE_CHECKING"
    )


def _imports(path: Path, *, include_type_checking: bool) -> set[str]:
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()

    def visit(statements: Iterable[ast.stmt], *, type_only: bool = False) -> None:
        for node in statements:
            if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
                visit(node.body, type_only=True)
                visit(node.orelse, type_only=type_only)
                continue
            if isinstance(node, ast.Import) and (include_type_checking or not type_only):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and (include_type_checking or not type_only):
                prefix = package.split(".")
                if node.level:
                    prefix = prefix[: len(prefix) - node.level + 1]
                base = ".".join([*prefix, node.module] if node.module else prefix)
                imports.add(base)
                imports.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt) and not isinstance(node, ast.If):
                    visit([child], type_only=type_only)

    visit(ast.parse(path.read_text(encoding="utf-8")).body)
    return imports


def _import_graph(*, include_type_checking: bool) -> dict[str, set[str]]:
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    modules = {_module_name(path): path for path in paths}
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for source, path in modules.items():
        if path.name == "__init__.py":
            continue
        for imported in _imports(path, include_type_checking=include_type_checking):
            target = imported
            while target not in modules and "." in target:
                target = target.rpartition(".")[0]
            if target in modules and target != source:
                graph[source].add(target)
    return graph


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[set[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def connect(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                connect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(component)

    for node in graph:
        if node not in indices:
            connect(node)
    return components


def _cycle_edges(graph: dict[str, set[str]], components: list[set[str]]) -> list[str]:
    return sorted(
        f"{source} -> {target}"
        for component in components
        for source in component
        for target in graph[source] & component
    )


def test_runtime_import_graph_has_no_cycles():
    graph = _import_graph(include_type_checking=False)
    components = _strongly_connected_components(graph)

    assert components == [], "runtime dependency cycles:\n" + "\n".join(_cycle_edges(graph, components))


def test_complete_import_graph_has_only_expected_type_debt():
    graph = _import_graph(include_type_checking=True)
    components = _strongly_connected_components(graph)
    expected_type_debt: set[frozenset[str]] = set()

    assert {frozenset(component) for component in components} == expected_type_debt, (
        "unexpected complete-graph dependency cycles:\n" + "\n".join(_cycle_edges(graph, components))
    )
