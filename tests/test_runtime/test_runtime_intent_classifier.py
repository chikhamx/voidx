import json
from pathlib import Path

import voidx.runtime.intent_classifier as intent_classifier_module

from voidx.runtime.intent import TaskIntent, infer_task_intent
from voidx.runtime.intent_classifier import (
    ArtifactClassifier,
    classify_intent,
    reset_intent_classifier_cache,
)


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "src" / "voidx" / "data" / "intent_classifier.json"


def test_classifier_returns_coarse_coding_intent_for_workspace_request():
    result = classify_intent("看看这个设计文档")

    assert result is not None
    assert result.intent == TaskIntent.CODING
    assert result.action == "accept"
    assert result.source == "keyword_classifier"


def test_classifier_missing_model_falls_back_to_keywords():
    result = classify_intent("看看 voidx 的 agent 编排", model_path=ROOT / "missing-intent-model.json")

    assert result is not None
    assert result.intent == TaskIntent.CODING
    assert result.action == "accept"
    assert result.source == "keyword_classifier"


def test_classifier_window_text_does_not_pollute_keyword_fallback():
    result = classify_intent("好了", classifier_text="修复这个bug [SEP] 好了")

    assert result is not None
    assert result.intent == TaskIntent.GENERAL
    assert result.source == "keyword_classifier"


def test_embedded_classifier_artifact_uses_coarse_intents():
    artifact = json.loads(MODEL.read_text(encoding="utf-8"))
    classifier = ArtifactClassifier(artifact)

    assert {intent.value for intent in classifier.classes} == {"coding", "general"}
    assert len(classifier.coef) == len(classifier.classes)
    assert len(classifier.intercept) == len(classifier.classes)


def test_classifier_fallback_uses_current_keyword_intent(monkeypatch):
    class FallbackClassifier:
        def classify(self, _text):
            raise AssertionError("coding keyword should bypass artifact classifier")

    monkeypatch.setattr(intent_classifier_module, "_load_classifier", lambda _model_path=None: FallbackClassifier())

    result = classify_intent("fix this issue")

    assert result is not None
    assert result.intent == TaskIntent.CODING
    assert result.source == "keyword_classifier"


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
    artifact = {
        "schema_version": 1,
        "model_type": "char_wb_tfidf_logreg",
        "classes": ["coding", "general"],
        "safe_accept_intents": ["coding", "general"],
        "vocabulary": {" x": 0},
        "idf": [1.0],
        "coef": [[1.0], [-1.0]],
        "intercept": [0.0, 0.0],
        "ngram_range": [2, 2],
    }

    for index in range(9):
        model_path = tmp_path / f"intent_classifier_{index}.json"
        _write_model(model_path, artifact)
        assert classify_intent("hello", model_path=model_path) is not None

    assert len(intent_classifier_module._CACHE) <= 8


def test_direct_write_request_sets_coding_feature_goal():
    assert infer_task_intent("开始实现这个优化", "auto") == TaskIntent.CODING


def test_plan_mode_forces_coding_design_goal():
    assert infer_task_intent("写一个新接口", "plan") == TaskIntent.CODING


def test_approval_phrase_without_pending_plan_requires_confirmation():
    assert infer_task_intent("对，可以", "auto") == TaskIntent.GENERAL


def test_chinese_and_mixed_input_classification_is_coarse_coding():
    chinese = classify_intent("为什么运行失败")
    mixed = classify_intent("review this PR")

    assert chinese is not None
    assert chinese.intent == TaskIntent.CODING
    assert mixed is not None
    assert mixed.intent == TaskIntent.CODING


def _write_model(path: Path, artifact: dict) -> None:
    path.write_text(json.dumps(artifact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
