"""Tree-structured output — OutputNode, OutputTree, box-drawing renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class OutputNode:
    """A single node in the output tree."""
    id: str
    parent: OutputNode | None = None
    children: list[OutputNode] = field(default_factory=list)
    depth: int = 0

    node_type: Literal["root", "turn", "tool_call", "tool_result", "subagent", "message", "error", "warn", "diff"] = "message"

    header: str = ""                   # Always visible (collapse summary)
    header_style: str = ""             # Rich style for header
    body_lines: list[str] = field(default_factory=list)  # Hidden when collapsed

    collapsed: bool = False
    status: Literal["running", "done", "error"] = "running"
    elapsed: float | None = None
    agent_name: str | None = None
    step_info: str | None = None

    _is_last_sibling: bool = False  # Set by parent when children are finalised

    @property
    def collapse_summary(self) -> str:
        """One-line summary shown when collapsed. Includes node ID in [nX] format."""
        parts = [f"[{self.id}]"]
        if self.node_type == "subagent" and self.agent_name:
            steps = self.step_info or ""
            elapsed = f"{self.elapsed:.1f}s" if self.elapsed else ""
            meta = " ".join(filter(None, [self.agent_name, steps, elapsed]))
            parts.append(f"\u25bc {meta}")
        elif self.node_type == "tool_call":
            parts.append(self.header)
            if self.elapsed:
                parts.append(f"({self.elapsed:.1f}s)")
        elif self.node_type == "tool_result":
            preview = self.header[:80] or (self.body_lines[0][:80] if self.body_lines else "")
            parts.append(preview)
        else:
            parts.append(self.header[:80] if self.header else "")

        return " ".join(parts)

    def add_child(self, child: OutputNode) -> None:
        """Add a child node, updating its parent, depth, and sibling flags."""
        child.parent = self
        child.depth = self.depth + 1
        # Update all existing children's _is_last_sibling
        for c in self.children:
            c._is_last_sibling = False
        child._is_last_sibling = True
        self.children.append(child)


class OutputTree:
    """Tree container with box-drawing renderer."""

    BOX_BRANCH = "\u251c\u2500\u2500"
    BOX_LAST   = "\u2514\u2500\u2500"
    BOX_VERT   = "\u2502  "
    BOX_SPACE  = "   "

    def __init__(self):
        self.root = OutputNode(id="root", node_type="root", depth=0)
        self._counter = 0
        self._all: dict[str, OutputNode] = {}  # id → node lookup
        self._click_map: dict[int, str] = {}   # backward compat

    def new_node(self, parent: OutputNode, **kwargs) -> OutputNode:
        """Create a new node under parent. Auto-assigns id."""
        self._counter += 1
        node = OutputNode(id=f"n{self._counter}", **kwargs)
        parent.add_child(node)
        self._all[node.id] = node
        return node

    def get(self, node_id: str) -> OutputNode | None:
        return self._all.get(node_id)

    def expand(self, node_id: str) -> OutputNode | None:
        node = self._all.get(node_id)
        if node:
            node.collapsed = False
        return node

    def collapse(self, node_id: str) -> OutputNode | None:
        node = self._all.get(node_id)
        if node:
            node.collapsed = True
        return node

    def expand_all(self) -> None:
        for node in self._all.values():
            node.collapsed = False

    def collapse_all(self, max_depth: int = 0) -> None:
        for node in self._all.values():
            if node.depth > max_depth:
                node.collapsed = True

    # ── rendering ──────────────────────────────────────────────────────────

    def render(self, console_width: int = 80) -> list[str]:
        """Flatten tree to lines with box-drawing characters.

        Returns list of plain strings (no Rich markup needed at this level —
        markup can be embedded in header/body_lines by the caller).
        """
        self._click_map.clear()
        lines: list[str] = []
        line_map: dict[int, str] = {}
        self._walk_render(self.root, [], lines, line_map)
        # Populate backward-compat _click_map
        self._click_map = dict(line_map)
        return lines

    def render_with_line_map(self, console_width: int = 80) -> tuple[list[str], dict[int, str]]:
        """Like render() but also returns a map of line_number → node_id
        for mouse click targeting."""
        self._click_map.clear()
        lines: list[str] = []
        line_map: dict[int, str] = {}
        self._walk_render(self.root, [], lines, line_map)
        self._click_map = dict(line_map)
        return lines, line_map

    def render_expanded(self, node_id: str, console_width: int = 80) -> list[str]:
        """Render a single collapsed node's subtree as an expanded view."""
        node = self._all.get(node_id)
        if not node:
            return []

        lines: list[str] = []
        # Title bar
        lines.append(f"  \u2500\u2500 [bold]Expanded [{node_id}][/bold] " + "\u2500" * (console_width - 20))

        # Temporarily expand this node for rendering
        was_collapsed = node.collapsed
        node.collapsed = False

        # Render children with box-drawing (like depth 1 nodes)
        new_parts = ["  "]
        for child in node.children:
            self._walk_render(child, new_parts, lines, None)

        node.collapsed = was_collapsed

        lines.append("  " + "\u2500" * (console_width - 4))
        return lines

    def click_at_row(self, row: int) -> str | None:
        return self._click_map.get(row)

    # ── internal walk ──────────────────────────────────────────────────────

    def _walk_render(self, node: OutputNode, prefix_parts: list[str],
                     lines: list[str], line_map: dict[int, str] | None = None) -> None:
        """Recursive depth-first walk to render the tree.

        Depth 0 (root):      not rendered, iterate children directly.
        Depth 1 (turns):     no box-drawing connector — header only.
        Depth 2+ (nested):   full box-drawing with ├── / └── connectors.
        """
        if node is self.root:
            for child in node.children:
                self._walk_render(child, [], lines, line_map)
            return

        # ── depth 1: no box-drawing ────────────────────────────────────
        if node.depth == 1:
            if node.collapsed:
                line = node.collapse_summary
                lines.append(line)
                if line_map is not None:
                    line_map[len(lines) - 1] = node.id
                return

            line = node.header if node.header else ""
            lines.append(line)
            if line_map is not None and node.node_type in ("subagent", "tool_call"):
                line_map[len(lines) - 1] = node.id

            # Body lines — plain spaces continuation
            for bl in node.body_lines:
                lines.append(f"  {bl}")

            # Children get box-drawing, indented under this node
            new_parts = ["  "]
            for child in node.children:
                self._walk_render(child, new_parts, lines, line_map)
            return

        # ── depth >= 2: full box-drawing ───────────────────────────────
        indent = "".join(prefix_parts)
        connector = self.BOX_LAST if node._is_last_sibling else self.BOX_BRANCH
        prefix = indent + connector

        if node.collapsed:
            line = f"{prefix} {node.collapse_summary}"
            lines.append(line)
            if line_map is not None:
                line_map[len(lines) - 1] = node.id
            return

        # Header line
        line = f"{prefix} {node.header}" if node.header else prefix
        lines.append(line)
        if line_map is not None and node.node_type in ("subagent", "tool_call"):
            line_map[len(lines) - 1] = node.id

        # Continuation for body lines and children
        cont_suffix = self.BOX_SPACE if node._is_last_sibling else self.BOX_VERT
        cont = indent + cont_suffix

        # Body lines
        for bl in node.body_lines:
            lines.append(f"{cont} {bl}")

        # Children
        new_parts = prefix_parts + [cont_suffix]
        for child in node.children:
            self._walk_render(child, new_parts, lines, line_map)
