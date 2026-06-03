"""Browse mode — mouse-driven tree expand/collapse after agent output."""

from __future__ import annotations

import sys
from rich.console import Console
from voidx.ui.tree import OutputTree


def browse(tree: OutputTree, console: Console) -> None:
    """Enter mouse browse mode. Click nodes to expand/collapse, any key to exit."""

    # Terminal escape sequences
    ENABLE_MOUSE = "\x1b[?1000h\x1b[?1006h"
    DISABLE_MOUSE = "\x1b[?1000l\x1b[?1006l"
    HINT = "[dim]点击节点展开/折叠，按任意键开始输入[/dim]"

    # Initial render — track line count for in-place updates
    width = console.width or 80
    lines, line_map = tree.render_with_line_map(width)
    for line in lines:
        console.print(line)
    console.print(HINT)
    total_lines = len(lines) + 1  # +1 for hint

    # Enable mouse tracking
    sys.stdout.write(ENABLE_MOUSE)
    sys.stdout.flush()

    try:
        if sys.platform == "win32":
            _browse_windows(tree, console, line_map, total_lines, width)
        else:
            _browse_unix(tree, console, line_map, total_lines, width)
    finally:
        sys.stdout.write(DISABLE_MOUSE)
        sys.stdout.flush()


def _rerender(tree, console, total_lines, width):
    """Move cursor up, clear, re-render tree + hint."""
    # Move up to start of tree
    sys.stdout.write(f"\x1b[{total_lines}A")
    sys.stdout.write("\x1b[J")  # Clear from cursor to end
    sys.stdout.flush()

    lines, line_map = tree.render_with_line_map(width)
    for line in lines:
        console.print(line)
    console.print("[dim]点击节点展开/折叠，按任意键开始输入[/dim]")
    sys.stdout.flush()
    return len(lines) + 1, line_map


def _browse_windows(tree, console, line_map, total_lines, width):
    import msvcrt

    while True:
        ch = msvcrt.getwch()
        if ch == '\x1b':
            # Check if mouse event follows: \x1b[<...
            if msvcrt.kbhit():
                ch2 = msvcrt.getwch()
                if ch2 == '[':
                    if msvcrt.kbhit():
                        ch3 = msvcrt.getwch()
                        if ch3 == '<':
                            # Parse SGR mouse: <button;col;row{M|m}
                            buf = ''
                            while True:
                                c = msvcrt.getwch()
                                if c in ('M', 'm'):
                                    break
                                buf += c
                            parts = buf.split(';')
                            if len(parts) >= 3 and parts[0] == '0':
                                # Left button press
                                try:
                                    row = int(parts[2])
                                    node_id = line_map.get(row - 1)  # 0-based
                                    if node_id:
                                        node = tree.get(node_id)
                                        if node:
                                            node.collapsed = not node.collapsed
                                            total_lines, line_map = _rerender(
                                                tree, console, total_lines, width)
                                except (ValueError, IndexError):
                                    pass
                        else:
                            # Arrow key or other sequence — exit
                            return
                    else:
                        return
                else:
                    return
            else:
                # Lone ESC — exit
                return
        else:
            # Any other key — exit
            return


def _browse_unix(tree, console, line_map, total_lines, width):
    import termios
    import tty
    import select

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            # Use select with short timeout to handle partial reads
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                continue

            ch = sys.stdin.buffer.read(1)
            if ch == b'\x1b':
                # Check for mouse/arrow sequence
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if ready:
                    ch2 = sys.stdin.buffer.read(1)
                    if ch2 == b'[':
                        ready2, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if ready2:
                            ch3 = sys.stdin.buffer.read(1)
                            if ch3 == b'<':
                                # Mouse event
                                buf = b''
                                while True:
                                    c = sys.stdin.buffer.read(1)
                                    if c in (b'M', b'm'):
                                        break
                                    buf += c
                                parts = buf.decode().split(';')
                                if len(parts) >= 3 and parts[0] == '0':
                                    try:
                                        row = int(parts[2])
                                        node_id = line_map.get(row - 1)
                                        if node_id:
                                            node = tree.get(node_id)
                                            if node:
                                                node.collapsed = not node.collapsed
                                                total_lines, line_map = _rerender(
                                                    tree, console, total_lines, width)
                                    except (ValueError, IndexError):
                                        pass
                            else:
                                return  # Arrow key → exit
                        else:
                            return  # [ with nothing → exit
                    else:
                        return  # not [ → ESC → exit
                else:
                    return  # Lone ESC → exit
            else:
                return  # Any other key → exit
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
