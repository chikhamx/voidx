from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "benchmark_cross_ui_performance.py"
REQUIRED_BENCHMARKS = {
    "tui_stream_incremental",
    "tui_stream_commit",
    "snapshot",
    "tui_rich_conversion",
    "paste",
    "terminal",
    "live_tree",
}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )




def _load_benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_cross_ui_performance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_benchmark_uses_production_paging_and_dto_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = _load_benchmark_module()
    calls = {"page": 0, "rows_to_tree": 0, "tree_to_snapshot": 0}

    for name, key in (
        ("_page_from_rows", "page"),
        ("transcript_rows_to_tree", "rows_to_tree"),
        ("tree_to_snapshot", "tree_to_snapshot"),
    ):
        original = getattr(benchmark, name)

        def spy(*args: object, _original=original, _key=key, **kwargs: object) -> object:
            calls[_key] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(benchmark, name, spy)

    measurement = benchmark._benchmark_snapshot(10_000, 1)

    assert calls == {"page": 1, "rows_to_tree": 1, "tree_to_snapshot": 1}
    assert measurement["history_nodes"] == 10_000
    assert measurement["page_node_count"] <= 40
    assert measurement["page_turns"] <= 40


def test_paste_content_equality_detects_reordered_parser_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = _load_benchmark_module()
    original = benchmark._PasteParserHarness._insert_pasted_text

    def reorder(self: object, text: str) -> None:
        original(self, text[::-1])

    monkeypatch.setattr(benchmark._PasteParserHarness, "_insert_pasted_text", reorder)

    measurement = benchmark._benchmark_paste(8192, 1)

    assert measurement["content_equal"] is False
def test_help_documents_supported_workload_and_output_options() -> None:
    result = _run("--help")

    assert result.returncode == 0, result.stderr
    for option in (
        "--stream-chars",
        "--snapshot-nodes",
        "--terminal-rate",
        "--iterations",
        "--json-output",
    ):
        assert option in result.stdout
    assert "synthetic" in result.stdout.lower()


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    output = tmp_path_factory.mktemp("cross-ui-benchmark") / "report.json"
    result = _run(
        "--stream-chars",
        "256",
        "--snapshot-nodes",
        "10000",
        "--terminal-rate",
        "4096",
        "--iterations",
        "2",
        "--json-output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert output.exists()
    return json.loads(output.read_text(encoding="utf-8"))


def test_json_report_has_stable_machine_and_measurement_schema(report: dict[str, object]) -> None:
    assert report["schema_version"] == 1
    assert all(report[key] for key in ("machine", "platform", "python_version"))
    assert isinstance(report["passed"], bool)
    assert isinstance(report["violations"], list)

    benchmarks = report["benchmarks"]
    assert isinstance(benchmarks, dict)
    assert set(benchmarks) == REQUIRED_BENCHMARKS
    for name, measurement in benchmarks.items():
        assert isinstance(measurement, dict), name
        assert len(measurement["samples"]) == 2, name
        assert all(isinstance(value, (int, float)) and value >= 0 for value in measurement["samples"]), name
        assert 0 <= measurement["p50_ms"] <= measurement["max_ms"], name
        assert 0 <= measurement["p95_ms"] <= measurement["max_ms"], name


def test_report_exposes_cross_ui_workload_contract(report: dict[str, object]) -> None:
    benchmarks = report["benchmarks"]

    incremental = benchmarks["tui_stream_incremental"]
    assert incremental["stream_chars"] == 256
    assert incremental["mutable_tail_bytes"] <= 16 * 1024

    commit = benchmarks["tui_stream_commit"]
    assert commit["canonical_equal"] is True
    assert commit["heartbeat_max_gap_ms"] >= 0
    assert commit["worker_p95_ms"] >= 0
    assert commit["install_p95_ms"] >= 0

    snapshot = benchmarks["snapshot"]
    assert snapshot["history_nodes"] == 10_000
    assert 1 <= snapshot["page_node_count"] <= 40
    assert snapshot["payload_bytes"] > 0
    assert 1 <= snapshot["page_turns"] <= 40
    assert snapshot["task_ms"] == snapshot["max_ms"]

    rich = benchmarks["tui_rich_conversion"]
    assert rich["rows"] == 30
    assert rich["conversion_count"] <= 62

    paste = benchmarks["paste"]
    assert paste["payload_bytes"] == 8 * 1024 * 1024
    assert paste["chunk_bytes"] == 4096
    assert paste["spooled"] is True
    assert paste["content_equal"] is True
    assert paste["peak_bytes"] >= paste["payload_bytes"]
    assert paste["peak_ratio"] == pytest.approx(
        paste["peak_bytes"] / paste["payload_bytes"],
        abs=1e-6,
    )

    terminal = benchmarks["terminal"]
    assert terminal["rate_bytes_per_second"] == 4096
    assert terminal["queue_bytes"] >= 0
    assert terminal["spool_bytes"] >= 0
    assert 0 <= terminal["heartbeat_max_gap_ms"] < 50
    assert terminal["byte_equal"] is True
    assert terminal["input_bytes"] == terminal["output_bytes"]
    assert terminal["drain_ticks"] > 0
    assert terminal["simulated_duration_ms"] == pytest.approx(
        terminal["input_bytes"] / terminal["rate_bytes_per_second"] * 1000,
    )

    live_tree = benchmarks["live_tree"]
    assert live_tree["retained_turns"] <= 20 or live_tree["projected_bytes"] <= 16 * 1024 * 1024


def test_default_output_is_json_on_stdout() -> None:
    result = _run(
        "--stream-chars",
        "64",
        "--snapshot-nodes",
        "20",
        "--terminal-rate",
        "1024",
        "--iterations",
        "1",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["benchmarks"]["snapshot"]["history_nodes"] == 20
