"""Train and evaluate the local task intent classifier.

The training step uses scikit-learn in the developer environment. The exported
artifact is JSON and is evaluated through the standard-library inference path
below so runtime integration does not need scikit-learn or pickle loading.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from voidx.runtime.intent_classifier import (
    DEFAULT_ACCEPT_CONFIDENCE,
    DEFAULT_SUGGEST_CONFIDENCE,
    ArtifactClassifier,
)

DEFAULT_TRAIN = ROOT / "data" / "intent" / "train.jsonl"
DEFAULT_EVAL = ROOT / "data" / "intent" / "eval.jsonl"
DEFAULT_MODEL = ROOT / "src" / "voidx" / "data" / "intent_classifier.json"
DEFAULT_REPORT = ROOT / "docs" / "reports" / "intent-classifier-eval.md"

INTENTS = ["coding", "general"]
INTENT_LABEL_ALIASES = {
    "coding": "coding",
    "general": "general",
    "chat": "general",
    "ambiguous": "general",
    "inspect": "coding",
    "design": "coding",
    "review": "coding",
    "implement": "coding",
    "debug": "coding",
}
SAFE_ACCEPT_INTENTS = ["coding", "general"]
NGRAM_RANGE = (2, 5)
MAX_FEATURES = 50_000


@dataclass(frozen=True)
class GateThresholds:
    max_model_size_kb: float = 1536.0
    max_avg_latency_ms: float = 1.0
    max_p95_latency_ms: float = 1.0
    min_macro_f1: float = 0.85
    min_coding_recall: float = 0.90
    min_general_precision: float = 0.80


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            intent = INTENT_LABEL_ALIASES.get(str(row.get("intent") or ""))
            if intent not in INTENTS:
                raise ValueError(f"{path}:{line_no} has invalid intent: {row.get('intent')!r}")
            row["intent"] = intent
            rows.append(row)
    return rows


def train_logistic_regression(train_rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Training requires scikit-learn in the developer environment. "
            "The exported runtime artifact does not depend on scikit-learn."
        ) from exc

    texts = [str(row["text"]) for row in train_rows]
    labels = [str(row["intent"]) for row in train_rows]
    pipeline = Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=NGRAM_RANGE,
                    max_features=MAX_FEATURES,
                    sublinear_tf=True,
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    max_iter=1000,
                    C=1.0,
                    class_weight="balanced",
                    solver="lbfgs",
                ),
            ),
        ]
    )
    pipeline.fit(texts, labels)

    vectorizer = pipeline.named_steps["tfidf"]
    classifier = pipeline.named_steps["clf"]
    classes = [str(value) for value in classifier.classes_]
    coef = [[float(value) for value in row] for row in classifier.coef_]
    intercept = [float(value) for value in classifier.intercept_]
    if len(classes) == 2 and len(coef) == 1 and len(intercept) == 1:
        positive_row = coef[0]
        positive_bias = intercept[0]
        coef = [[-value for value in positive_row], positive_row]
        intercept = [-positive_bias, positive_bias]

    return {
        "schema_version": 1,
        "model_type": "char_wb_tfidf_logreg",
        "created_at": datetime.now(UTC).isoformat(),
        "classes": classes,
        "safe_accept_intents": SAFE_ACCEPT_INTENTS,
        "decision_thresholds": {
            "accept_confidence": DEFAULT_ACCEPT_CONFIDENCE,
            "suggest_confidence": DEFAULT_SUGGEST_CONFIDENCE,
        },
        "ngram_range": list(NGRAM_RANGE),
        "max_features": MAX_FEATURES,
        "normalization": {
            "lowercase": True,
            "analyzer": "char_wb",
            "sublinear_tf": True,
            "norm": "l2",
            "smooth_idf": True,
        },
        "vocabulary": {str(key): int(value) for key, value in vectorizer.vocabulary_.items()},
        "idf": [float(value) for value in vectorizer.idf_],
        "coef": coef,
        "intercept": intercept,
    }


def write_artifact(artifact: dict[str, Any], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = encode_artifact(artifact)
    path.write_bytes(encoded)
    return len(encoded)


def encode_artifact(artifact: dict[str, Any]) -> bytes:
    return json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def load_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_artifact(artifact: dict[str, Any], eval_rows: list[dict[str, Any]]) -> dict[str, Any]:
    classifier = ArtifactClassifier(artifact)
    predictions = []
    confidences = []
    labels = []
    for row in eval_rows:
        prediction, confidence = classifier.predict(str(row["text"]))
        predictions.append(prediction.value)
        confidences.append(confidence)
        labels.append(str(row["intent"]))

    return evaluate_predictions(labels, predictions, confidences)


def evaluate_predictions(labels: list[str], predictions: list[str], confidences: list[float]) -> dict[str, Any]:
    total = len(labels)
    correct = sum(1 for expected, actual in zip(labels, predictions, strict=True) if expected == actual)
    per_label = {}
    f1_values = []
    recall_values = {}
    confusion = {intent: {other: 0 for other in INTENTS} for intent in INTENTS}
    for expected, actual in zip(labels, predictions, strict=True):
        confusion[expected][actual] += 1

    for intent in INTENTS:
        tp = sum(1 for expected, actual in zip(labels, predictions, strict=True) if expected == intent and actual == intent)
        fp = sum(1 for expected, actual in zip(labels, predictions, strict=True) if expected != intent and actual == intent)
        fn = sum(1 for expected, actual in zip(labels, predictions, strict=True) if expected == intent and actual != intent)
        support = sum(1 for expected in labels if expected == intent)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[intent] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
        f1_values.append(f1)
        recall_values[intent] = recall

    general_precision = per_label["general"]["precision"]
    coding_recall = recall_values["coding"]
    return {
        "accuracy": round(correct / total if total else 0.0, 4),
        "macro_f1": round(sum(f1_values) / len(f1_values), 4),
        "coding_recall": round(coding_recall, 4),
        "general_precision": general_precision,
        "per_label": per_label,
        "confusion_matrix": [[confusion[row][col] for col in INTENTS] for row in INTENTS],
        "avg_confidence": round(statistics.mean(confidences) if confidences else 0.0, 4),
    }


def measure_latency(artifact: dict[str, Any], rows: list[dict[str, Any]], runs: int) -> dict[str, float]:
    classifier = ArtifactClassifier(artifact)
    texts = [str(row["text"]) for row in rows]
    if not texts:
        return {"avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}

    latencies = []
    for _ in range(max(1, runs)):
        for text in texts:
            start = time.perf_counter()
            classifier.predict(text)
            latencies.append((time.perf_counter() - start) * 1000)

    sorted_latencies = sorted(latencies)
    p95_index = max(0, math.ceil(len(sorted_latencies) * 0.95) - 1)
    return {
        "avg_ms": round(statistics.mean(latencies), 4),
        "p95_ms": round(sorted_latencies[p95_index], 4),
        "max_ms": round(max(latencies), 4),
    }


def gate_results(metrics: dict[str, Any], thresholds: GateThresholds | None = None) -> dict[str, dict[str, Any]]:
    limits = thresholds or GateThresholds()
    checks = {
        "model_size": {
            "value": metrics["model_size_kb"],
            "required": f"<= {limits.max_model_size_kb}",
            "passed": metrics["model_size_kb"] <= limits.max_model_size_kb,
        },
        "average_latency": {
            "value": metrics["latency"]["avg_ms"],
            "required": f"<= {limits.max_avg_latency_ms}",
            "passed": metrics["latency"]["avg_ms"] <= limits.max_avg_latency_ms,
        },
        "p95_latency": {
            "value": metrics["latency"]["p95_ms"],
            "required": f"<= {limits.max_p95_latency_ms}",
            "passed": metrics["latency"]["p95_ms"] <= limits.max_p95_latency_ms,
        },
        "macro_f1": {
            "value": metrics["evaluation"]["macro_f1"],
            "required": f">= {limits.min_macro_f1}",
            "passed": metrics["evaluation"]["macro_f1"] >= limits.min_macro_f1,
        },
        "coding_recall": {
            "value": metrics["evaluation"]["coding_recall"],
            "required": f">= {limits.min_coding_recall}",
            "passed": metrics["evaluation"]["coding_recall"] >= limits.min_coding_recall,
        },
        "general_precision": {
            "value": metrics["evaluation"]["general_precision"],
            "required": f">= {limits.min_general_precision}",
            "passed": metrics["evaluation"]["general_precision"] >= limits.min_general_precision,
        },
    }
    return checks


def gate_passed(metrics: dict[str, Any], thresholds: GateThresholds | None = None) -> bool:
    return all(check["passed"] for check in gate_results(metrics, thresholds).values())


def write_report(metrics: dict[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    evaluation = metrics["evaluation"]
    latency = metrics["latency"]
    gates = gate_results(metrics)
    lines = [
        "# Intent Classifier Evaluation Report",
        "",
        f"Date: {metrics['created_at']}",
        "",
        "## Summary",
        "",
        "| Metric | Value | Gate | Passed |",
        "|--------|-------|------|--------|",
        f"| Accuracy | {evaluation['accuracy']} | informational | yes |",
        f"| Macro F1 | {evaluation['macro_f1']} | {gates['macro_f1']['required']} | {_mark(gates['macro_f1']['passed'])} |",
        f"| Coding recall | {evaluation['coding_recall']} | {gates['coding_recall']['required']} | {_mark(gates['coding_recall']['passed'])} |",
        f"| General precision | {evaluation['general_precision']} | {gates['general_precision']['required']} | {_mark(gates['general_precision']['passed'])} |",
        f"| Average inference latency (ms) | {latency['avg_ms']} | {gates['average_latency']['required']} | {_mark(gates['average_latency']['passed'])} |",
        f"| p95 inference latency (ms) | {latency['p95_ms']} | {gates['p95_latency']['required']} | {_mark(gates['p95_latency']['passed'])} |",
        f"| Model artifact size (KB) | {metrics['model_size_kb']} | {gates['model_size']['required']} | {_mark(gates['model_size']['passed'])} |",
        "",
        "## Inputs",
        "",
        f"- Train data: `{metrics['train_path']}` ({metrics['train_rows']} rows)",
        f"- Eval data: `{metrics['eval_path']}` ({metrics['eval_rows']} rows)",
        f"- Model artifact: `{metrics['model_path']}`",
        "- Training model: character n-gram TF-IDF logistic regression",
        "- Evaluation path: exported JSON artifact with pure-Python inference",
        "",
        "## Per-Label Metrics",
        "",
        "| Intent | Precision | Recall | F1 | Support |",
        "|--------|-----------|--------|----|---------|",
    ]
    for intent in INTENTS:
        row = evaluation["per_label"][intent]
        lines.append(f"| {intent} | {row['precision']} | {row['recall']} | {row['f1']} | {row['support']} |")

    lines.extend(
        [
            "",
            "## Confusion Matrix",
            "",
            "| Actual \\ Predicted | " + " | ".join(INTENTS) + " |",
            "|" + "---|" * (len(INTENTS) + 1),
        ]
    )
    for intent, row in zip(INTENTS, evaluation["confusion_matrix"], strict=True):
        lines.append(f"| {intent} | " + " | ".join(str(value) for value in row) + " |")

    lines.extend(
        [
            "",
            "## Gate Result",
            "",
            "Passed." if gate_passed(metrics) else "Failed.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def _mark(value: bool) -> str:
    return "yes" if value else "no"


def build_metrics(
    train_path: Path,
    eval_path: Path,
    model_path: Path,
    report_path: Path,
    latency_runs: int,
) -> dict[str, Any]:
    train_rows = load_jsonl(train_path)
    eval_rows = load_jsonl(eval_path)
    artifact = train_logistic_regression(train_rows)
    artifact["training"] = {
        "train_path": str(train_path.relative_to(ROOT) if train_path.is_relative_to(ROOT) else train_path),
        "eval_path": str(eval_path.relative_to(ROOT) if eval_path.is_relative_to(ROOT) else eval_path),
        "train_rows": len(train_rows),
        "eval_rows": len(eval_rows),
    }
    evaluation = evaluate_artifact(artifact, eval_rows)
    latency = measure_latency(artifact, eval_rows, latency_runs)
    metrics = {
        "created_at": datetime.now(UTC).isoformat(),
        "train_path": str(train_path.relative_to(ROOT) if train_path.is_relative_to(ROOT) else train_path),
        "eval_path": str(eval_path.relative_to(ROOT) if eval_path.is_relative_to(ROOT) else eval_path),
        "model_path": str(model_path.relative_to(ROOT) if model_path.is_relative_to(ROOT) else model_path),
        "report_path": str(report_path.relative_to(ROOT) if report_path.is_relative_to(ROOT) else report_path),
        "train_rows": len(train_rows),
        "eval_rows": len(eval_rows),
        "model_size_kb": 0.0,
        "evaluation": evaluation,
        "latency": latency,
    }
    artifact["metrics"] = metrics
    for _ in range(3):
        artifact["gate"] = gate_results(metrics)
        model_size_kb = round(len(encode_artifact(artifact)) / 1024, 1)
        if model_size_kb == metrics["model_size_kb"]:
            break
        metrics["model_size_kb"] = model_size_kb
        artifact["metrics"]["model_size_kb"] = model_size_kb
    artifact["gate"] = gate_results(metrics)
    write_artifact(artifact, model_path)
    write_report(metrics, report_path)
    return metrics


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--report-out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--latency-runs", type=int, default=5)
    parser.add_argument("--no-gate", action="store_true", help="write outputs even if metrics do not pass")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    metrics = build_metrics(args.train, args.eval, args.model_out, args.report_out, args.latency_runs)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if not args.no_gate and not gate_passed(metrics):
        print(f"intent classifier evaluation gate failed; see {args.report_out}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
