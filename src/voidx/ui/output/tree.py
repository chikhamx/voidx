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
        "todo",
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
            return self.header
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
        self._line_map: dict[int, str] = {}    # rendered line → owning node id
        self._click_map: dict[int, str] = {}   # rendered line → clickable node id
        self._dirty: bool = True
        self._dirty_nodes: set[str] = set()
        self._node_ranges: dict[str, tuple[int, int]] = {}  # id → (start, end)
        self._node_prefixes: dict[str, list[str]] = {}  # id → prefix_parts
        self._cached_lines: list[str] = []
        self._cached_width: int = 0

    def startup_line_count(self) -> int:
        """Return the number of rendered lines occupied by startup nodes."""
        count = 0
        for child in self.root.children:
            if child.node_type == "startup":
                rng = self._node_ranges.get(child.id)
                if rng:
                    count += rng[1] - rng[0]
                else:
                    count += 1 + len(child.body_lines)
        return count

    def mark_dirty(self, node_id: str | None = None) -> None:
        if node_id is None:
            self._dirty = True
            self._dirty_nodes.clear()
        else:
            self._dirty_nodes.add(node_id)

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
        if not self._dirty and not self._dirty_nodes and self._cached_width == console_width:
            return self._cached_lines

        # Full render: structural change or width change
        if self._dirty or self._cached_width != console_width:
            return self._full_render(console_width)

        # Incremental: only content changes on existing nodes
        return self._incremental_render(console_width)

    def _full_render(self, console_width: int) -> list[str]:
        self._line_map.clear()
        self._click_map.clear()
        self._node_ranges.clear()
        self._node_prefixes.clear()
        lines: list[str] = []
        line_map: dict[int, str] = {}
        click_map: dict[int, str] = {}
        self._walk_render(self.root, [], lines, line_map, click_map)
        self._line_map = dict(line_map)
        self._click_map = dict(click_map)
        self._cached_lines = lines
        self._cached_width = console_width
        self._dirty = False
        self._dirty_nodes.clear()
        return lines

    def _incremental_render(self, console_width: int) -> list[str]:
        """Incremental render: re-walk only dirty subtrees, splice into cache.

        Assumes a single content-only dirty node (e.g. streaming text appended
        to the current assistant node).  Multiple independent dirty nodes or
        structural changes fall back to full render via ``_dirty``.

        After splice, ancestor and sibling-after ``_node_ranges`` are repaired
        via a delta pass, and ``_click_map`` is rebuilt from pre-splice entries
        (shifted) plus the re-walked subtree entries (offset to absolute rows).
        """
        if not self._dirty_nodes:
            return self._cached_lines

        dirty_with_ranges = [
            (nid, self._node_ranges.get(nid, (0, 0)))
            for nid in self._dirty_nodes
            if nid in self._node_ranges and nid in self._all
        ]
        if not dirty_with_ranges:
            self._dirty_nodes.clear()
            return self._cached_lines

        # Filter out nodes whose ancestors are also dirty
        dirty_set = self._dirty_nodes
        independent: list[str] = []
        for nid, _ in sorted(dirty_with_ranges, key=lambda x: x[1][0]):
            node = self._all.get(nid)
            if node is None:
                continue
            ancestor_dirty = False
            cursor = node.parent
            while cursor and cursor is not self.root:
                if cursor.id in dirty_set:
                    ancestor_dirty = True
                    break
                cursor = cursor.parent
            if not ancestor_dirty:
                independent.append(nid)

        if not independent:
            self._dirty_nodes.clear()
            return self._cached_lines

        # If multiple independent dirty nodes, fall back to full render
        if len(independent) > 1:
            self._dirty = True
            self._dirty_nodes.clear()
            return self.render(console_width)

        # Single dirty node: render its subtree, splice into cache
        nid = independent[0]
        old_start, old_end = self._node_ranges[nid]
        node = self._all[nid]

        # Capture pre-walk keys so we can shift newly written ranges.
        # _walk_render writes ranges relative to new_lines start (0).
        pre_keys = set(self._node_ranges.keys())

        new_lines: list[str] = []
        prefix = self._node_prefixes.get(nid, [])
        sub_line_map: dict[int, str] = {}
        sub_click_map: dict[int, str] = {}
        self._walk_render(node, prefix, new_lines, sub_line_map, sub_click_map)

        # Shift ranges written by _walk_render from relative → absolute.
        new_keys = set(self._node_ranges.keys()) - pre_keys
        changed_keys = {
            k for k in (set(self._node_ranges.keys()) & pre_keys)
            if self._node_ranges[k][0] < old_start
        }
        for r_nid in new_keys | changed_keys:
            s, e = self._node_ranges[r_nid]
            self._node_ranges[r_nid] = (s + old_start, e + old_start)

        # Splice: everything before + new subtree + everything after
        self._cached_lines = (
            self._cached_lines[:old_start]
            + new_lines
            + self._cached_lines[old_end:]
        )
        # Fix stale ranges: ancestors and sibling-after nodes shifted by delta.
        delta = len(new_lines) - (old_end - old_start)
        if delta != 0:
            for r_nid, (s, e) in list(self._node_ranges.items()):
                if s >= old_end:
                    self._node_ranges[r_nid] = (s + delta, e + delta)
                elif e > old_end and s < old_start:
                    self._node_ranges[r_nid] = (s, e + delta)
        self._dirty_nodes.clear()

        # Rebuild maps: old entries outside the splice range shift or stay,
        # new entries from the re-walked subtree replace the spliced range.
        def rebuild_map(old_map: dict[int, str], sub_map: dict[int, str]) -> dict[int, str]:
            rebuilt: dict[int, str] = {}
            for row, row_nid in old_map.items():
                if row < old_start:
                    rebuilt[row] = row_nid
                elif row >= old_end:
                    rebuilt[row + delta] = row_nid
            for row, row_nid in sub_map.items():
                rebuilt[row + old_start] = row_nid
            return rebuilt

        self._line_map = rebuild_map(self._line_map, sub_line_map)
        self._click_map = rebuild_map(self._click_map, sub_click_map)

        return self._cached_lines

    def render_with_line_map(self, console_width: int = 80) -> tuple[list[str], dict[int, str]]:
        """Like render() but also returns line_number → owning node_id."""
        self.render(console_width)
        return self._cached_lines, self._line_map

    def render_with_click_map(self, console_width: int = 80) -> tuple[list[str], dict[int, str]]:
        """Like render() but also returns line_number → clickable node_id."""
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

    def _walk_render(
        self,
        node: OutputNode,
        prefix_parts: list[str],
        lines: list[str],
        line_map: dict[int, str] | None = None,
        click_map: dict[int, str] | None = None,
    ) -> None:
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
                self._walk_render(child, [], lines, line_map, click_map)
            return

        start = len(lines)

        if _is_transparent_agent_subagent(node):
            for child in node.children:
                self._walk_render(child, prefix_parts, lines, line_map, click_map)
            self._node_ranges[node.id] = (start, len(lines))
            self._node_prefixes[node.id] = list(prefix_parts)
            return

        # ── depth 1: no box-drawing ────────────────────────────────────
        if node.depth == 1:
            if node.collapsed:
                line = node.collapse_summary
                lines.append(line)
                if line_map is not None:
                    line_map[len(lines) - 1] = node.id
                if click_map is not None:
                    click_map[len(lines) - 1] = node.id
                self._node_ranges[node.id] = (start, len(lines))
                self._node_prefixes[node.id] = []
                return

            line = node.header if node.header else ""
            lines.append(line)
            if line_map is not None:
                line_map[len(lines) - 1] = node.id
            if click_map is not None and _is_clickable(node):
                click_map[len(lines) - 1] = node.id

            body_prefix = "  " if node.node_type == "turn" else ""
            for bl in node.body_lines:
                lines.append(f"{body_prefix}{bl}")
                if line_map is not None:
                    line_map[len(lines) - 1] = node.id

            # Children get box-drawing, indented under this node
            new_parts = [" "]
            for child in node.children:
                self._walk_render(child, new_parts, lines, line_map, click_map)
            self._node_ranges[node.id] = (start, len(lines))
            self._node_prefixes[node.id] = []
            return

        # ── depth >= 2: full box-drawing ───────────────────────────────
        indent = "".join(prefix_parts)
        effectively_last = _is_effectively_last_sibling(node)
        connector = self.BOX_LAST if effectively_last else self.BOX_BRANCH
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
            if click_map is not None:
                click_map[len(lines) - 1] = node.id
            self._node_ranges[node.id] = (start, len(lines))
            self._node_prefixes[node.id] = list(prefix_parts)
            return

        # Header line
        current_prefix = indent if inline_tool_result else aligned_prefix
        line = f"{current_prefix}{node.header}" if node.header else current_prefix
        lines.append(line)
        if line_map is not None:
            line_map[len(lines) - 1] = node.id
        if click_map is not None and _is_clickable(node):
            click_map[len(lines) - 1] = node.id

        # Continuation for body lines and children
        cont_suffix = self.BOX_SPACE if suppress_connector or effectively_last else self.BOX_VERT
        cont = indent if inline_tool_result else indent + cont_suffix

        # Body lines
        for bl in node.body_lines:
            lines.append(f"{cont}{bl}")
            if line_map is not None:
                line_map[len(lines) - 1] = node.id
            if click_map is not None and _is_clickable(node):
                click_map[len(lines) - 1] = node.id

        # Children
        new_parts = prefix_parts if inline_tool_result else prefix_parts + [cont_suffix]
        for child in node.children:
            self._walk_render(child, new_parts, lines, line_map, click_map)
        self._node_ranges[node.id] = (start, len(lines))
        self._node_prefixes[node.id] = list(prefix_parts)


def _is_clickable(node: OutputNode) -> bool:
    return node.node_type in {"subagent", "tool_call", "tool_result", "thought", "status"}


def _is_effectively_last_sibling(node: OutputNode) -> bool:
    parent = node.parent
    if parent is None:
        return True
    siblings = parent.children
    try:
        index = siblings.index(node)
    except ValueError:
        return node._is_last_sibling
    return not any(not _is_inline_tool_result(sibling) for sibling in siblings[index + 1:])


def _is_inline_tool_result(node: OutputNode) -> bool:
    return (
        node.node_type == "tool_result"
        and node.parent is not None
        and node.parent.node_type == "tool_call"
    )


def _is_transparent_agent_subagent(node: OutputNode) -> bool:
    parent = node.parent
    return (
        node.node_type == "subagent"
        and parent is not None
        and parent.node_type == "tool_call"
        and parent.payload.get("tool_name") == "agent"
    )
