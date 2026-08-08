from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
PACKAGE_ROOT = SOURCE_ROOT / "voidx"
DEBT_PATH = ROOT / "src" / "tests" / "fixtures" / "architecture" / "current_edges.json"


@dataclass(frozen=True)
class ImportRef:
    source: str
    target: str
    line: int
    type_checking: bool
    dynamic: bool = False


def module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
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


def _literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_relative(
    source: str,
    level: int,
    imported: str | None,
    *,
    source_is_package: bool,
) -> str:
    source_parts = source.split(".")
    package_parts = source_parts if source_is_package else source_parts[:-1]
    if level > len(package_parts) + 1:
        return imported or ""
    base = package_parts[: len(package_parts) - level + 1]
    if imported:
        base.extend(imported.split("."))
    return ".".join(base)


def _walk_imports(
    source: str,
    statements: list[ast.stmt],
    *,
    source_is_package: bool,
    type_checking: bool = False,
) -> list[ImportRef]:
    result: list[ImportRef] = []
    for statement in statements:
        if isinstance(statement, ast.If) and _is_type_checking_guard(statement.test):
            result.extend(
                _walk_imports(
                    source,
                    statement.body,
                    source_is_package=source_is_package,
                    type_checking=True,
                )
            )
            result.extend(
                _walk_imports(
                    source,
                    statement.orelse,
                    source_is_package=source_is_package,
                    type_checking=type_checking,
                )
            )
            continue
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "voidx" or alias.name.startswith("voidx."):
                    result.append(
                        ImportRef(source, alias.name, statement.lineno, type_checking)
                    )
        elif isinstance(statement, ast.ImportFrom):
            target = (
                _resolve_relative(
                    source,
                    statement.level,
                    statement.module,
                    source_is_package=source_is_package,
                )
                if statement.level
                else statement.module or ""
            )
            if target == "voidx" or target.startswith("voidx."):
                result.append(
                    ImportRef(source, target, statement.lineno, type_checking)
                )
                for alias in statement.names:
                    if alias.name != "*":
                        result.append(
                            ImportRef(
                                source,
                                f"{target}.{alias.name}",
                                statement.lineno,
                                type_checking,
                            )
                        )
        for call in (node for node in ast.walk(statement) if isinstance(node, ast.Call)):
            function = call.func
            is_import_module = (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
                and function.attr == "import_module"
            )
            is_dunder_import = isinstance(function, ast.Name) and function.id == "__import__"
            if (is_import_module or is_dunder_import) and call.args:
                target = _literal_string(call.args[0])
                if target and (target == "voidx" or target.startswith("voidx.")):
                    result.append(
                        ImportRef(
                            source,
                            target,
                            call.lineno,
                            type_checking,
                            dynamic=True,
                        )
                    )
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt) and not isinstance(statement, ast.If):
                result.extend(
                    _walk_imports(
                        source,
                        [child],
                        source_is_package=source_is_package,
                        type_checking=type_checking,
                    )
                )
    return result


def imports_for(path: Path, *, include_type_checking: bool = True) -> list[ImportRef]:
    source = module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    refs = _walk_imports(
        source,
        tree.body,
        source_is_package=path.name == "__init__.py",
    )
    if not include_type_checking:
        refs = [ref for ref in refs if not ref.type_checking]
    return refs


def module_paths() -> dict[str, Path]:
    return {
        module_name(path): path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    }


def _existing_module(target: str, modules: dict[str, Path]) -> str | None:
    candidate = target
    while candidate and candidate not in modules:
        candidate = candidate.rpartition(".")[0]
    return candidate or None


def import_edges(*, include_type_checking: bool = True) -> list[ImportRef]:
    modules = module_paths()
    edges: set[ImportRef] = set()
    for path in modules.values():
        for ref in imports_for(path, include_type_checking=include_type_checking):
            target = _existing_module(ref.target, modules)
            if target and target != ref.source:
                edges.add(
                    ImportRef(
                        ref.source,
                        target,
                        ref.line,
                        ref.type_checking,
                        ref.dynamic,
                    )
                )
    return sorted(edges, key=lambda ref: (ref.source, ref.target, ref.line))


def import_graph(*, include_type_checking: bool = True) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {module: set() for module in module_paths()}
    for edge in import_edges(include_type_checking=include_type_checking):
        graph[edge.source].add(edge.target)
    return graph


def graph_from_edges(edges: list[ImportRef]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {module: set() for module in module_paths()}
    for edge in edges:
        graph[edge.source].add(edge.target)
    return graph


def strongly_connected_components(graph: dict[str, set[str]]) -> list[set[str]]:
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


def load_debt() -> set[tuple[str, str, str]]:
    if not DEBT_PATH.exists():
        return set()
    return {
        (item["source"], item["target"], item["rule"])
        for item in json.loads(DEBT_PATH.read_text(encoding="utf-8"))
    }


def is_debt(edge: ImportRef, rule: str) -> bool:
    return (edge.source, edge.target, rule) in load_debt()


def without_debt(edges: list[ImportRef], rule: str) -> list[ImportRef]:
    return [edge for edge in edges if not is_debt(edge, rule)]


def format_edges(edges: list[ImportRef]) -> str:
    return "\n".join(
        f"  source: {edge.source} (line {edge.line})\n"
        f"  target: {edge.target}\n"
        f"  dynamic: {edge.dynamic}"
        for edge in edges
    )


def top_level(module: str) -> str:
    return module.split(".")[1] if module.startswith("voidx.") else module


def is_under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")
