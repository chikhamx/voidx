"""LangGraph topology construction boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class LangGraphTopology:
    def __init__(self, builder: Callable[[Any], Any]) -> None:
        self._builder = builder

    def build(self, nodes: Any) -> Any:
        return self._builder(nodes)
