from __future__ import annotations

from .import_graph import (
    graph_from_edges,
    import_edges,
    strongly_connected_components,
    without_debt,
)


def _cycle_edges(graph: dict[str, set[str]], components: list[set[str]]) -> list[str]:
    return sorted(
        f"{source} -> {target}"
        for component in components
        for source in component
        for target in graph[source] & component
    )


def test_runtime_import_graph_has_no_cycles():
    graph = graph_from_edges(
        without_debt(import_edges(include_type_checking=False), "runtime_cycle")
    )
    components = strongly_connected_components(graph)
    assert components == [], "runtime dependency cycles:\n" + "\n".join(
        _cycle_edges(graph, components)
    )


def test_type_checking_import_graph_has_no_cycles():
    graph = graph_from_edges(
        without_debt(import_edges(include_type_checking=True), "type_checking_cycle")
    )
    components = strongly_connected_components(graph)
    assert components == [], "complete dependency cycles:\n" + "\n".join(
        _cycle_edges(graph, components)
    )
