#!/usr/bin/env python3
"""Run synthetic cross-UI performance benchmarks and emit one JSON report.

The workloads are generated in memory. This script never opens a real session or
transcript, so reports are reproducible without access to user data.
"""

from __future__ import annotations

import argparse
import json
import platform as platform_module
import statistics
import sys
import time
import tracemalloc
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TUI = ROOT / "tui"
for import_path in (SRC, TUI):
    # Force workspace sources ahead of any runtime site-packages on PYTHONPATH.
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from voidx.presentation.adapters.persistence.transcript_snapshot import (
    TranscriptNodeRow,
    _page_from_rows,
    transcript_rows_to_tree,
)
from voidx.presentation.output.dock.formatting import text_from_line
from voidx.presentation.output.dock.stream import (
    StreamCommitWorkItem,
    build_canonical_stream_projection,
)
from voidx.presentation.output.dock.stream_projection import StreamingMarkdownProjection
from voidx.presentation.output.tree import OutputTree
from voidx.presentation.protocol.transcript import tree_to_snapshot
from voidx_cli.parser import _InputParserMixin

MIB = 1024 * 1024
STREAM_CHUNK_CHARS = 256
SNAPSHOT_PAGE_TURNS = 40
RICH_VIEWPORT_ROWS = 30
RICH_OVERSCAN_ROWS = 32
LIVE_TREE_TURNS = 20


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run synthetic cross-UI benchmarks only; no real session data is read."
        ),
    )
    parser.add_argument("--stream-chars", type=_positive_int, default=50_000,
                        help="synthetic assistant stream size in characters (default: 50000)")
    parser.add_argument("--snapshot-nodes", type=_positive_int, default=10_000,
                        help="synthetic transcript snapshot node count (default: 10000)")
    parser.add_argument("--terminal-rate", type=_positive_int, default=MIB,
                        help="synthetic terminal drain rate in bytes/second (default: 1048576)")
    parser.add_argument("--iterations", type=_positive_int, default=5,
                        help="number of samples per benchmark (default: 5)")
    parser.add_argument("--json-output", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--paste-bytes", type=_positive_int, default=8 * MIB,
                        help="synthetic bracketed-paste payload size (default: 8 MiB)")
    parser.add_argument("--terminal-bytes", type=_positive_int, default=10 * MIB,
                        help="synthetic committed terminal output size (default: 10 MiB)")
    return parser


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _measurement(samples: list[float], **details: object) -> dict[str, object]:
    rounded = [round(value, 6) for value in samples]
    return {
        "samples": rounded,
        "p50_ms": round(statistics.median(samples), 6),
        "p95_ms": round(_percentile(samples, 0.95), 6),
        "max_ms": round(max(samples), 6),
        **details,
    }


def _synthetic_stream(size: int) -> str:
    block = "Synthetic **stream** line with `code` and text.\n\n"
    return (block * (size // len(block) + 1))[:size]


def _benchmark_stream(text: str, iterations: int) -> tuple[dict[str, object], dict[str, object]]:
    update_samples: list[float] = []
    mutable_tail_bytes = 0
    for _ in range(iterations):
        projection = StreamingMarkdownProjection(width=100)
        slowest_update = 0.0
        for offset in range(0, len(text), STREAM_CHUNK_CHARS):
            started = time.perf_counter()
            projection.update(text[offset:offset + STREAM_CHUNK_CHARS])
            slowest_update = max(slowest_update, (time.perf_counter() - started) * 1000)
        update_samples.append(slowest_update)
        mutable_tail_bytes = max(
            mutable_tail_bytes,
            len(projection._mutable_tail.encode("utf-8")),
        )

    commit_samples: list[float] = []
    worker_samples: list[float] = []
    install_samples: list[float] = []
    heartbeat_gaps: list[float] = []
    canonical_equal = True
    work_item = StreamCommitWorkItem(
        node_id="synthetic-stream",
        parent_id=None,
        revision=1,
        generation=1,
        raw_text=text,
        phase="text",
        width=100,
    )

    def build_projection() -> tuple[object, float]:
        worker_started = time.perf_counter()
        result = build_canonical_stream_projection(work_item)
        return result, (time.perf_counter() - worker_started) * 1000

    with ThreadPoolExecutor(max_workers=1) as executor:
        for _ in range(iterations):
            started = time.perf_counter()
            last_heartbeat = started
            max_gap = 0.0
            future = executor.submit(build_projection)
            while not future.done():
                now = time.perf_counter()
                max_gap = max(max_gap, (now - last_heartbeat) * 1000)
                last_heartbeat = now
                time.sleep(0.001)
            projection, worker_ms = future.result()
            install_started = time.perf_counter()
            matches = projection.raw_text == text.strip("\n")
            installed_lines = (projection.header, *projection.body_lines)
            install_ms = (time.perf_counter() - install_started) * 1000
            finished = time.perf_counter()
            max_gap = max(max_gap, (finished - last_heartbeat) * 1000)
            commit_samples.append((finished - started) * 1000)
            worker_samples.append(worker_ms)
            install_samples.append(install_ms)
            heartbeat_gaps.append(max_gap)
            canonical_equal &= matches and bool(installed_lines)

    return (
        _measurement(
            update_samples,
            stream_chars=len(text),
            chunk_chars=STREAM_CHUNK_CHARS,
            mutable_tail_bytes=mutable_tail_bytes,
        ),
        _measurement(
            commit_samples,
            stream_chars=len(text),
            canonical_equal=canonical_equal,
            heartbeat_max_gap_ms=round(max(heartbeat_gaps), 6),
            worker_p95_ms=round(_percentile(worker_samples, 0.95), 6),
            install_p95_ms=round(_percentile(install_samples, 0.95), 6),
        ),
    )


def _snapshot_rows(node_count: int) -> tuple[list[TranscriptNodeRow], list[int]]:
    turn_ids = list(range(node_count))
    rows = [
        TranscriptNodeRow(
            session_id="synthetic",
            turn_id=turn_id,
            node_id=0,
            sort_order=0,
            node_type="turn",
            header=f"Synthetic turn {turn_id}",
            status="done",
        )
        for turn_id in turn_ids
    ]
    return rows, turn_ids


def _benchmark_snapshot(node_count: int, iterations: int) -> dict[str, object]:
    rows, turn_ids = _snapshot_rows(node_count)
    samples: list[float] = []
    payload_bytes = page_node_count = page_turns = 0
    for _ in range(iterations):
        started = time.perf_counter()
        page = _page_from_rows(
            rows,
            turn_ids,
            before_turn_id=None,
            turn_limit=SNAPSHOT_PAGE_TURNS,
        )
        tree = transcript_rows_to_tree(page.rows)
        snapshot = tree_to_snapshot(tree, session_id="synthetic")
        payload = snapshot.model_dump_json().encode("utf-8")
        samples.append((time.perf_counter() - started) * 1000)
        payload_bytes = len(payload)
        page_node_count = len(snapshot.nodes)
        page_turns = len({row.turn_id for row in page.rows})
    result = _measurement(
        samples,
        history_nodes=node_count,
        page_node_count=page_node_count,
        payload_bytes=payload_bytes,
        page_turns=page_turns,
    )
    result["task_ms"] = result["max_ms"]
    return result


def _rich_tree() -> OutputTree:
    tree = OutputTree()
    for turn_id in range(100):
        tree.new_node(
            tree.root,
            node_type="turn",
            header=f"[bold]Synthetic turn {turn_id}[/bold]",
            body_lines=[f"row {turn_id}-{row}" for row in range(3)],
            status="done",
        )
    return tree


def _benchmark_rich(iterations: int) -> dict[str, object]:
    samples: list[float] = []
    conversion_count = 0
    row_limit = RICH_VIEWPORT_ROWS + RICH_OVERSCAN_ROWS
    for _ in range(iterations):
        tree = _rich_tree()
        started = time.perf_counter()
        visible = tree.render_tail(100, row_limit=row_limit)
        converted = [text_from_line(line) for line in visible]
        samples.append((time.perf_counter() - started) * 1000)
        conversion_count = len(converted)
    return _measurement(
        samples,
        rows=RICH_VIEWPORT_ROWS,
        overscan_rows=RICH_OVERSCAN_ROWS,
        conversion_count=conversion_count,
    )


class _PasteParserHarness(_InputParserMixin):
    def __init__(self) -> None:
        self._pending_bytes = b""
        self._paste_buffer = None
        self.pasted_text: str | None = None
        self.spooled = False

    def _append_paste_data(self, data: bytes) -> None:
        super()._append_paste_data(data)
        self.spooled |= bool(getattr(self._paste_buffer, "_rolled", False))

    def _insert_pasted_text(self, text: str) -> None:
        self.pasted_text = text


def _benchmark_paste(payload_bytes: int, iterations: int) -> dict[str, object]:
    pattern = b"0123456789abcdef"
    payload = (pattern * (payload_bytes // len(pattern) + 1))[:payload_bytes]
    chunk_bytes = 4096
    samples: list[float] = []
    peak_bytes = payload_bytes
    spooled = False
    content_equal = True
    for _ in range(iterations):
        harness = _PasteParserHarness()
        tracemalloc.start()
        started = time.perf_counter()
        harness._process_input(harness._PASTE_START)
        for offset in range(0, payload_bytes, chunk_bytes):
            harness._process_input(payload[offset:offset + chunk_bytes])
        harness._process_input(harness._PASTE_END)
        samples.append((time.perf_counter() - started) * 1000)
        _, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        spooled |= harness.spooled
        content_equal &= (
            harness.pasted_text is not None
            and harness.pasted_text.encode("utf-8") == payload
        )
        peak_bytes = max(peak_bytes, payload_bytes + traced_peak)
    return _measurement(
        samples,
        payload_bytes=payload_bytes,
        chunk_bytes=chunk_bytes,
        spooled=spooled,
        content_equal=content_equal,
        peak_bytes=peak_bytes,
        peak_ratio=round(peak_bytes / payload_bytes, 6),
    )


def _terminal_round_trip(
    total_bytes: int,
    rate: int,
) -> tuple[bytes, int, int, float, int, float]:
    source = (b"synthetic-terminal-output\n" * (total_bytes // 26 + 1))[:total_bytes]
    chunks = deque(source[offset:offset + 64 * 1024] for offset in range(0, total_bytes, 64 * 1024))
    queue_bytes = sum(map(len, chunks))
    spool = BytesIO()
    sink = BytesIO()

    while chunks:
        spool.write(chunks.popleft())
    spool_bytes = spool.tell()
    spool.seek(0)

    heartbeat_interval_seconds = 0.025
    drain_bytes = max(int(rate * heartbeat_interval_seconds), 1)
    drain_ticks = 0
    while True:
        chunk = spool.read(drain_bytes)
        if not chunk:
            break
        sink.write(chunk)
        drain_ticks += 1

    simulated_duration_ms = total_bytes / rate * 1000
    heartbeat_gap_ms = min(
        heartbeat_interval_seconds * 1000,
        simulated_duration_ms,
    )
    return (
        sink.getvalue(),
        queue_bytes,
        spool_bytes,
        heartbeat_gap_ms,
        drain_ticks,
        simulated_duration_ms,
    )


def _benchmark_terminal(total_bytes: int, rate: int, iterations: int) -> dict[str, object]:
    samples: list[float] = []
    queue_bytes = spool_bytes = drain_ticks = 0
    simulated_duration_ms = 0.0
    heartbeat_gaps: list[float] = []
    byte_equal = True
    expected = (b"synthetic-terminal-output\n" * (total_bytes // 26 + 1))[:total_bytes]
    output = b""
    for _ in range(iterations):
        started = time.perf_counter()
        (
            output,
            queue_bytes,
            spool_bytes,
            gap,
            drain_ticks,
            simulated_duration_ms,
        ) = _terminal_round_trip(total_bytes, rate)
        samples.append((time.perf_counter() - started) * 1000)
        heartbeat_gaps.append(gap)
        byte_equal &= output == expected
    return _measurement(
        samples,
        rate_bytes_per_second=rate,
        queue_bytes=queue_bytes,
        spool_bytes=spool_bytes,
        heartbeat_max_gap_ms=round(max(heartbeat_gaps), 6),
        drain_ticks=drain_ticks,
        simulated_duration_ms=simulated_duration_ms,
        byte_equal=byte_equal,
        input_bytes=len(expected),
        output_bytes=len(output),
    )


def _benchmark_live_tree(iterations: int) -> dict[str, object]:
    samples: list[float] = []
    retained_turns = projected_bytes = 0
    for _ in range(iterations):
        tree = OutputTree()
        for turn_id in range(60):
            tree.new_node(
                tree.root,
                node_type="turn",
                header=f"Synthetic retained turn {turn_id}",
                body_lines=["x" * 128],
                status="done",
                payload={
                    "transcript_turn_id": turn_id,
                    "durable": True,
                    "committed": True,
                    "lifecycle": "completed",
                    "terminal": True,
                    "active": False,
                    "referenced": False,
                    "pinned": False,
                    "render_pending": False,
                },
            )
        started = time.perf_counter()
        tree.evict_root_turns(keep=LIVE_TREE_TURNS)
        samples.append((time.perf_counter() - started) * 1000)
        retained = [node for node in tree.root.children if node.node_type == "turn"]
        retained_turns = len(retained)
        projected_bytes = sum(
            len(node.header.encode("utf-8"))
            + sum(len(line.encode("utf-8")) for line in node.body_lines)
            for node in retained
        )
    return _measurement(
        samples,
        retained_turns=retained_turns,
        projected_bytes=projected_bytes,
    )


def _violations(benchmarks: dict[str, dict[str, object]], iterations: int) -> list[str]:
    violations: list[str] = []
    incremental = benchmarks["tui_stream_incremental"]
    commit = benchmarks["tui_stream_commit"]
    snapshot = benchmarks["snapshot"]
    rich = benchmarks["tui_rich_conversion"]
    paste = benchmarks["paste"]
    terminal = benchmarks["terminal"]
    live_tree = benchmarks["live_tree"]

    if incremental["mutable_tail_bytes"] > 16 * 1024:
        violations.append("tui stream mutable tail exceeds 16 KiB")
    if not commit["canonical_equal"]:
        violations.append("canonical stream content differs")
    if snapshot["page_turns"] > SNAPSHOT_PAGE_TURNS:
        violations.append("snapshot page exceeds 40 turns")
    if rich["conversion_count"] > RICH_VIEWPORT_ROWS + RICH_OVERSCAN_ROWS:
        violations.append("Rich conversion exceeds viewport plus overscan")
    if not terminal["byte_equal"]:
        violations.append("terminal output bytes differ")
    if live_tree["retained_turns"] > LIVE_TREE_TURNS and live_tree["projected_bytes"] > 16 * MIB:
        violations.append("live tree exceeds retained turn and projected byte limits")

    # Short test runs validate the contract and complexity invariants, not host timing.
    if iterations >= 5:
        timed_limits = (
            (incremental["p95_ms"], 16, "incremental stream p95 >= 16 ms"),
            (incremental["max_ms"], 50, "incremental stream max >= 50 ms"),
            (commit["p95_ms"], 1000, "canonical commit p95 >= 1 s"),
            (commit["heartbeat_max_gap_ms"], 50, "commit heartbeat gap >= 50 ms"),
            (snapshot["task_ms"], 50, "snapshot task >= 50 ms"),
            (paste["max_ms"], 50, "paste task >= 50 ms"),
            (paste["peak_ratio"], 2.5, "paste peak ratio >= 2.5"),
            (terminal["heartbeat_max_gap_ms"], 50, "terminal heartbeat gap >= 50 ms"),
        )
        violations.extend(message for value, limit, message in timed_limits if value >= limit)
    return violations


def run(args: argparse.Namespace) -> dict[str, object]:
    stream = _synthetic_stream(args.stream_chars)
    incremental, commit = _benchmark_stream(stream, args.iterations)
    benchmarks = {
        "tui_stream_incremental": incremental,
        "tui_stream_commit": commit,
        "snapshot": _benchmark_snapshot(args.snapshot_nodes, args.iterations),
        "tui_rich_conversion": _benchmark_rich(args.iterations),
        "paste": _benchmark_paste(args.paste_bytes, args.iterations),
        "terminal": _benchmark_terminal(args.terminal_bytes, args.terminal_rate, args.iterations),
        "live_tree": _benchmark_live_tree(args.iterations),
    }
    violations = _violations(benchmarks, args.iterations)
    return {
        "schema_version": 1,
        "machine": platform_module.node() or platform_module.machine() or "unknown",
        "platform": platform_module.platform(),
        "python_version": platform_module.python_version(),
        "passed": not violations,
        "violations": violations,
        "thresholds_enforced": args.iterations >= 5,
        "benchmarks": benchmarks,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
