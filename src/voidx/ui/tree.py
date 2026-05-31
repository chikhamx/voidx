"""Tree-structured output — OutputNode, OutputTree, box-drawing renderer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from typing import Literal


@dataclass
class OutputNode:
    """A single node in the output tree."""
    id: str
    parent: OutputNode | None = None
    children: list[OutputNode] = field(default_factory=list)
    depth: int = 0

    node_type: Literal[
        "root",
        "startup",
        "turn",
        "tool_call",
        "tool_result",
        "subagent",
        "message",
        "assistant",
        "thought",
        "status",
        "permission",
        "error",
        "warn",
        "diff",
    ] = "message"

    header: str = ""                   # Always visible (collapse summary)
    header_style: str = ""             # Rich style for header
    body_lines: list[str] = field(default_factory=list)  # Hidden when collapsed

    collapsed: bool = False
    status: Literal["running", "done", "error"] = "running"
    elapsed: float | None = None
    agent_name: str | None = None
    step_info: str | None = None
    meta: str | None = None
    tool_call_id: str | None = None
    agent_run_id: str | None = None
    message_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    _is_last_sibling: bool = False  # Set by parent when children are finalised

    @property
    def collapse_summary(self) -> str:
        """One-line summary shown when collapsed."""
        if self.node_type == "thought" and self.meta:
            return self.meta
        elif self.node_type == "status":
            return self.header[:120] if self.header else self.meta or ""
        elif self.node_type == "subagent" and self.agent_name:
            steps = self.step_info or ""
            elapsed = f"{self.elapsed:.1f}s" if self.elapsed else ""
            meta = " ".join(filter(None, [self.agent_name, steps, elapsed]))
            return f"\u25bc {meta}"
        elif self.node_type == "tool_call":
            parts = [self.header]
            if self.elapsed:
                parts.append(f"({self.elapsed:.1f}s)")
            return " ".join(parts)
        elif self.node_type == "tool_result":
            preview = self.header[:80] or (self.body_lines[0][:80] if self.body_lines else "")
            return preview
        else:
            return self.header[:80] if self.header else ""

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

    BOX_BRANCH = "[dim]├─[/dim] "
    BOX_LAST   = "[dim]└─[/dim] "
    BOX_VERT   = "   "
    BOX_SPACE  = "   "

    def __init__(self):
        self.root = OutputNode(id="root", node_type="root", depth=0)
        self._counter = 0
        self._all: dict[str, OutputNode] = {}  # id → node lookup
        self._click_map: dict[int, str] = {}   # backward compat
        self._dirty: bool = True
        self._cached_lines: list[str] = []
        self._cached_width: int = 0

    def mark_dirty(self) -> None:
        self._dirty = True

    def new_node(self, parent: OutputNode, *, node_id: str | None = None, **kwargs) -> OutputNode:
        """Create a new node under parent. Auto-assigns id."""
        if node_id is None:
            self._counter += 1
            node_id = f"n{self._counter}"
        node = OutputNode(id=node_id, **kwargs)
        self.add_node(parent, node)
        self._sync_counter(node.id)
        return node

    def add_node(self, parent: OutputNode, node: OutputNode) -> None:
        """Attach an existing node and register its subtree."""
        parent.add_child(node)
        self._register_subtree(node)
        self._sync_counter(node.id)
        self.mark_dirty()

    def extend_from(self, other: OutputTree) -> None:
        """Append another tree's root children to this tree."""
        for child in list(other.root.children):
            self.add_node(self.root, child)

    def _register_subtree(self, node: OutputNode) -> None:
        self._all[node.id] = node
        for child in node.children:
            child.parent = node
            child.depth = node.depth + 1
            self._register_subtree(child)

    def _sync_counter(self, node_id: str) -> None:
        match = re.fullmatch(r"n(\d+)", node_id)
        if match:
            self._counter = max(self._counter, int(match.group(1)))

    def get(self, node_id: str) -> OutputNode | None:
        return self._all.get(node_id)

    def expand(self, node_id: str) -> OutputNode | None:
        node = self._all.get(node_id)
        if node:
            node.collapsed = False
            self.mark_dirty()
        return node

    def collapse(self, node_id: str) -> OutputNode | None:
        node = self._all.get(node_id)
        if node:
            node.collapsed = True
            self.mark_dirty()
        return node

    def expand_all(self) -> None:
        for node in self._all.values():
            node.collapsed = False
        self.mark_dirty()

    def collapse_all(self, max_depth: int = 0) -> None:
        for node in self._all.values():
            if node.depth > max_depth:
                node.collapsed = True
        self.mark_dirty()

    # ── rendering ──────────────────────────────────────────────────────────

    def render(self, console_width: int = 80) -> list[str]:
        """Flatten tree to lines with box-drawing characters.

        Returns list of plain strings (no Rich markup needed at this level —
        markup can be embedded in header/body_lines by the caller).
        """
        if not self._dirty and self._cached_width == console_width:
            return self._cached_lines

        self._click_map.clear()
        lines: list[str] = []
        line_map: dict[int, str] = {}
        self._walk_render(self.root, [], lines, line_map)
        # Populate backward-compat _click_map
        self._click_map = dict(line_map)

        self._cached_lines = lines
        self._cached_width = console_width
        self._dirty = False
        return lines

    def render_with_line_map(self, console_width: int = 80) -> tuple[list[str], dict[int, str]]:
        """Like render() but also returns a map of line_number → node_id
        for mouse click targeting."""
        self.render(console_width)
        return self._cached_lines, self._click_map

    def render_expanded(self, node_id: str, console_width: int = 80) -> list[str]:
        """Render a single collapsed node's subtree as an expanded view."""
        node = self._all.get(node_id)
        if not node:
            return []

        lines: list[str] = []
        # Title bar
        lines.append(f"  \u2500\u2500 [bold]Expanded[/bold] " + "\u2500" * (console_width - 13))

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
        Depth 2+ (nested):   dim connector on the first header, spaces after that.
        """
        if node is self.root:
            prev = None
            for child in node.children:
                if prev is not None and prev.node_type == "turn" and child.node_type == "message" and child.header:
                    lines.append("")
                prev = child
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
            if line_map is not None and _is_clickable(node):
                line_map[len(lines) - 1] = node.id

            # Body lines — plain spaces continuation
            for bl in node.body_lines:
                lines.append(bl)

            # Children get box-drawing, indented under this node
            new_parts = [" "]
            for child in node.children:
                self._walk_render(child, new_parts, lines, line_map)
            return

        # ── depth >= 2: full box-drawing ───────────────────────────────
        indent = "".join(prefix_parts)
        connector = self.BOX_LAST if node._is_last_sibling else self.BOX_BRANCH
        is_first_sibling = (
            node.parent is not None
            and bool(node.parent.children)
            and node.parent.children[0] is node
        )
        suppress_connector = node.parent is not None and node.parent.node_type == "assistant"
        prefix = indent + connector
        aligned_prefix = indent + self.BOX_SPACE if suppress_connector else (
            prefix if is_first_sibling else indent + self.BOX_SPACE
        )
        inline_tool_result = (
            node.node_type == "tool_result"
            and node.parent is not None
            and node.parent.node_type == "tool_call"
        )

        if node.collapsed:
            line = f"{indent if inline_tool_result else aligned_prefix}{node.collapse_summary}"
            lines.append(line)
            if line_map is not None:
                line_map[len(lines) - 1] = node.id
            return

        # Header line
        current_prefix = indent if inline_tool_result else aligned_prefix
        line = f"{current_prefix}{node.header}" if node.header else current_prefix
        lines.append(line)
        if line_map is not None and _is_clickable(node):
            line_map[len(lines) - 1] = node.id

        # Continuation for body lines and children
        cont_suffix = self.BOX_SPACE if suppress_connector or node._is_last_sibling else self.BOX_VERT
        cont = indent if inline_tool_result else indent + cont_suffix

        # Body lines
        for bl in node.body_lines:
            lines.append(f"{cont}{bl}")

        # Children
        new_parts = prefix_parts if inline_tool_result else prefix_parts + [cont_suffix]
        for child in node.children:
            self._walk_render(child, new_parts, lines, line_map)


def _is_clickable(node: OutputNode) -> bool:
    return node.node_type in {"subagent", "tool_call", "tool_result", "thought", "status"}
