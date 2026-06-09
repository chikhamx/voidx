import json
import time
from pathlib import Path

import voidx.runtime.intent_classifier as intent_classifier_module

from voidx.agent.task_state import TaskState, resolve_turn_intent
from voidx.runtime.intent import TaskIntent
from voidx.runtime.intent_classifier import (
    IntentClassifierResult,
    classify_intent,
    reset_intent_classifier_cache,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "src" / "voidx" / "data" / "intent_classifier.json"


def test_classifier_high_confidence_safe_intent():
    result = classify_intent("看看这个设计文档")

    assert result is not None
    assert result.intent == TaskIntent.INSPECT
    assert result.action == "accept"
    assert result.source == "local_classifier"


def test_classifier_missing_model_falls_back():
    result = classify_intent("看看 voidx 的 agent 编排", model_path=ROOT / "missing-intent-model.json")

    assert result is not None
    assert result.intent == TaskIntent.INSPECT
    assert result.action == "accept"
    assert result.source == "keyword_classifier"


def test_classifier_low_confidence_uses_keyword_fallback(tmp_path):
    artifact = json.loads(MODEL.read_text(encoding="utf-8"))
    artifact["decision_thresholds"] = {
        "accept_confidence": 1.01,
        "suggest_confidence": 1.01,
    }
    model_path = tmp_path / "intent_classifier.json"
    _write_model(model_path, artifact)

    result = classify_intent("看看 voidx 的 agent 编排", model_path=model_path)

    assert result is not None
    assert result.intent == TaskIntent.INSPECT
    assert result.action == "accept"
    assert result.source == "keyword_classifier"


def test_classifier_window_text_does_not_pollute_keyword_fallback():
    result = classify_intent("好了", classifier_text="修复这个bug [SEP] 好了")

    assert result is not None
    assert result.intent == TaskIntent.CHAT
    assert result.source == "keyword_classifier"


def test_classifier_fallback_can_return_keyword_ambiguous(monkeypatch):
    class FallbackClassifier:
        def classify(self, _text):
            return IntentClassifierResult(
                intent=TaskIntent.DESIGN,
                confidence=0.0,
                action="fallback",
            )

    monkeypatch.setattr(intent_classifier_module, "_load_classifier", lambda _model_path=None: FallbackClassifier())
    monkeypatch.setattr(
        intent_classifier_module,
        "infer_task_intent",
        lambda _text, _mode=None: TaskIntent.AMBIGUOUS,
    )

    result = classify_intent("这个", classifier_text="看看这个 bug [SEP] 这个")

    assert result is not None
    assert result.intent == TaskIntent.AMBIGUOUS
    assert result.action == "accept"
    assert result.source == "keyword_classifier"


def test_classifier_reload_after_model_replacement(tmp_path):
    reset_intent_classifier_cache()
    model_path = tmp_path / "intent_classifier.json"
    artifact = json.loads(MODEL.read_text(encoding="utf-8"))
    _write_model(model_path, artifact)

    first = classify_intent("hello", model_path=model_path)

    assert first is not None
    assert first.intent == TaskIntent.CHAT

    replacement = json.loads(MODEL.read_text(encoding="utf-8"))
    replacement["intercept"] = [-100.0 for _ in replacement["classes"]]
    replacement["intercept"][replacement["classes"].index("review")] = 100.0
    replacement["replacement_marker"] = "force signature change"
    time.sleep(0.01)
    _write_model(model_path, replacement)

    second = classify_intent("hello", model_path=model_path)

    assert second is not None
    assert second.intent == TaskIntent.REVIEW
    assert second.source == "local_classifier"


def test_embedded_model_signature_uses_content_hash(monkeypatch):
    class FakeResource:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def joinpath(self, _name: str):
            return self

        def read_bytes(self) -> bytes:
            return self.content

    monkeypatch.setattr(intent_classifier_module.resources, "files", lambda _package: FakeResource(b"same-size-a"))
    first = intent_classifier_module._locate_model(None)
    monkeypatch.setattr(intent_classifier_module.resources, "files", lambda _package: FakeResource(b"same-size-b"))
    second = intent_classifier_module._locate_model(None)

    assert first.signature != second.signature


def test_classifier_cache_is_bounded(tmp_path):
    reset_intent_classifier_cache()
    artifact = json.loads(MODEL.read_text(encoding="utf-8"))

    for index in range(9):
        model_path = tmp_path / f"intent_classifier_{index}.json"
        _write_model(model_path, artifact)
        assert classify_intent("hello", model_path=model_path) is not None

    assert len(intent_classifier_module._CACHE) <= 8


def test_classifier_implement_prediction_is_suggest_only():
    result = classify_intent("写一个新接口")

    assert result is not None
    assert result.intent == TaskIntent.IMPLEMENT
    assert result.action == "suggest"
    assert result.source == "local_classifier"


def test_resolve_turn_intent_requires_confirmation_for_ml_implement_prediction():
    resolution = resolve_turn_intent("写一个新接口", "auto", TaskState())

    assert resolution.intent == TaskIntent.AMBIGUOUS
    assert "local classifier suggested implement" in resolution.reason


def test_keyword_implement_preserves_existing_behavior():
    resolution = resolve_turn_intent("开始实现这个优化", "auto", TaskState())

    assert resolution.intent == TaskIntent.IMPLEMENT
    assert resolution.reason == "keyword classifier matched implement"


def test_plan_mode_ignores_classifier():
    resolution = resolve_turn_intent("写一个新接口", "plan", TaskState())

    assert resolution.intent == TaskIntent.DESIGN
    assert resolution.reason == "interaction mode forces design"


def test_approval_phrase_without_pending_plan_stays_ambiguous():
    resolution = resolve_turn_intent("对，可以", "auto", TaskState())

    assert resolution.intent == TaskIntent.AMBIGUOUS
    assert resolution.reason == "approval phrase without a pending implementation plan"


def test_chinese_and_mixed_input_classification():
    chinese = classify_intent("为什么运行失败")
    mixed = classify_intent("review this PR")

    assert chinese is not None
    assert chinese.intent == TaskIntent.DEBUG
    assert mixed is not None
    assert mixed.intent == TaskIntent.REVIEW


def _write_model(path: Path, artifact: dict) -> None:
    path.write_text(json.dumps(artifact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
