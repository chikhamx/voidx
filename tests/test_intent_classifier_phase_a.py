import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "train_intent_classifier.py"
MODEL = ROOT / "src" / "voidx" / "data" / "intent_classifier.json"
REPORT = ROOT / "docs" / "reports" / "intent-classifier-eval.md"
EVAL = ROOT / "data" / "intent" / "eval.jsonl"


def _load_training_module():
    spec = importlib.util.spec_from_file_location("train_intent_classifier", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_training_script_writes_model(tmp_path):
    pytest.importorskip("sklearn")
    train_rows = [
        ("你好", "chat"),
        ("thanks", "chat"),
        ("看看这个模块", "inspect"),
        ("explain this function", "inspect"),
        ("给个方案", "design"),
        ("suggest an approach", "design"),
        ("review this PR", "review"),
        ("审查一下代码", "review"),
        ("修复这个bug", "implement"),
        ("fix this issue", "implement"),
        ("这里报错了", "debug"),
        ("why is this failing", "debug"),
        ("这个", "ambiguous"),
        ("what now", "ambiguous"),
    ]
    eval_rows = train_rows[:]
    train_path = tmp_path / "train.jsonl"
    eval_path = tmp_path / "eval.jsonl"
    model_path = tmp_path / "intent_classifier.json"
    report_path = tmp_path / "intent-classifier-eval.md"
    _write_jsonl(train_path, train_rows)
    _write_jsonl(eval_path, eval_rows)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--train",
            str(train_path),
            "--eval",
            str(eval_path),
            "--model-out",
            str(model_path),
            "--report-out",
            str(report_path),
            "--latency-runs",
            "1",
            "--no-gate",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(model_path.read_text(encoding="utf-8"))
    assert artifact["model_type"] == "char_wb_tfidf_logreg"
    assert artifact["classes"]
    assert report_path.is_file()


def test_eval_report_contains_required_metrics():
    text = REPORT.read_text(encoding="utf-8")

    for phrase in [
        "Accuracy",
        "Macro F1",
        "Implement false positives",
        "Inspect/Design recall",
        "Average inference latency",
        "p95 inference latency",
        "Model artifact size",
        "Per-Label Metrics",
        "Confusion Matrix",
    ]:
        assert phrase in text


def test_eval_gate_rejects_implement_false_positive():
    module = _load_training_module()
    metrics = {
        "model_size_kb": 1.0,
        "latency": {"avg_ms": 0.1, "p95_ms": 0.1},
        "evaluation": {
            "macro_f1": 0.99,
            "implement_false_positives": 1,
            "inspect_design_recall": 0.99,
        },
    }

    checks = module.gate_results(metrics)

    assert checks["implement_false_positives"]["passed"] is False
    assert module.gate_passed(metrics) is False


def test_model_artifact_size():
    assert MODEL.stat().st_size <= 1536 * 1024


def test_classifier_latency():
    module = _load_training_module()
    artifact = module.load_artifact(MODEL)
    rows = module.load_jsonl(EVAL)

    latency = module.measure_latency(artifact, rows, runs=1)

    assert latency["avg_ms"] <= 1.0
    assert latency["p95_ms"] <= 1.0


def test_phase_a_artifact_metrics_pass_gate():
    module = _load_training_module()
    artifact = module.load_artifact(MODEL)

    assert module.gate_passed(artifact["metrics"]) is True
    assert artifact["metrics"]["evaluation"]["implement_false_positives"] == 0


def test_resolve_turn_intent_keeps_design_behavior_after_classifier_integration():
    from voidx.agent.task_state import TaskState, resolve_turn_intent

    resolution = resolve_turn_intent("给个优化方案", "auto", TaskState())

    assert resolution.intent.value == "design"
    assert "classifier matched design" in resolution.reason


def test_package_data_includes_intent_classifier():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert "intent_classifier.json" in package_data["voidx.data"]


def _write_jsonl(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for text, intent in rows:
            handle.write(json.dumps({"text": text, "intent": intent}, ensure_ascii=False) + "\n")
