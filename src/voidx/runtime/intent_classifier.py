"""Runtime loader and pure-Python inference for task intent classification."""

from __future__ import annotations

import hashlib
import json
import math
import threading

from collections import Counter
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent


DEFAULT_MODEL_RESOURCE = "intent_classifier.json"
DEFAULT_ACCEPT_CONFIDENCE = 0.55
DEFAULT_SUGGEST_CONFIDENCE = 0.50
DEFAULT_NGRAM_RANGE = (2, 5)
_SUPPORTED_MODEL_TYPE = "char_wb_tfidf_logreg"
_MAX_CACHE_ENTRIES = 8


class IntentClassifierResult(BaseModel):
    intent: TaskIntent
    confidence: float
    source: str = "local_classifier"
    action: Literal["accept", "suggest", "fallback"]


@dataclass(frozen=True)
class _ModelLocation:
    key: str
    signature: tuple[Any, ...]
    path: Path | None = None
    content: bytes | None = None


@dataclass(frozen=True)
class _CacheEntry:
    signature: tuple[Any, ...]
    classifier: "ArtifactClassifier"


class ArtifactClassifier:
    def __init__(self, artifact: dict[str, Any]) -> None:
        self.schema_version = int(artifact.get("schema_version", 0))
        self.model_type = str(artifact.get("model_type", ""))
        self.classes = [TaskIntent(str(item)) for item in artifact["classes"]]
        self.safe_accept_intents = {
            TaskIntent(str(item))
            for item in artifact.get("safe_accept_intents", [])
            if str(item) != TaskIntent.IMPLEMENT.value
        }
        if not self.safe_accept_intents:
            self.safe_accept_intents = {intent for intent in TaskIntent if intent != TaskIntent.IMPLEMENT}

        self.vocabulary = {str(key): int(value) for key, value in artifact["vocabulary"].items()}
        self.idf = [float(value) for value in artifact["idf"]]
        self.coef = [[float(value) for value in row] for row in artifact["coef"]]
        self.intercept = [float(value) for value in artifact["intercept"]]
        ngram_range = artifact.get("ngram_range", list(DEFAULT_NGRAM_RANGE))
        self.ngram_range = (int(ngram_range[0]), int(ngram_range[1]))
        thresholds = artifact.get("decision_thresholds", {})
        self.accept_confidence = float(thresholds.get("accept_confidence", DEFAULT_ACCEPT_CONFIDENCE))
        self.suggest_confidence = float(thresholds.get("suggest_confidence", DEFAULT_SUGGEST_CONFIDENCE))
        self._validate()

    def classify(self, text: str) -> IntentClassifierResult:
        intent, confidence = self.predict(text)
        if intent == TaskIntent.IMPLEMENT:
            action: Literal["accept", "suggest", "fallback"] = (
                "suggest" if confidence >= self.suggest_confidence else "fallback"
            )
        elif intent in self.safe_accept_intents and confidence >= self.accept_confidence:
            action = "accept"
        else:
            action = "fallback"
        return IntentClassifierResult(intent=intent, confidence=confidence, action=action)

    def predict(self, text: str) -> tuple[TaskIntent, float]:
        values = self._tfidf_values(text)
        scores = []
        for row, bias in zip(self.coef, self.intercept, strict=True):
            score = bias
            for index, value in values:
                score += row[index] * value
            scores.append(score)

        best_index = max(range(len(scores)), key=scores.__getitem__)
        max_score = max(scores)
        exps = [math.exp(score - max_score) for score in scores]
        confidence = exps[best_index] / sum(exps)
        return self.classes[best_index], confidence

    def _tfidf_values(self, text: str) -> list[tuple[int, float]]:
        counts: Counter[int] = Counter()
        for gram in char_wb_ngrams(text, self.ngram_range):
            index = self.vocabulary.get(gram)
            if index is not None:
                counts[index] += 1

        values = []
        norm_sq = 0.0
        for index, count in counts.items():
            value = (1.0 + math.log(count)) * self.idf[index]
            values.append((index, value))
            norm_sq += value * value

        if not values:
            return []

        norm = math.sqrt(norm_sq) or 1.0
        return [(index, value / norm) for index, value in values]

    def _validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported intent classifier schema: {self.schema_version}")
        if self.model_type != _SUPPORTED_MODEL_TYPE:
            raise ValueError(f"unsupported intent classifier model type: {self.model_type}")
        if not self.classes:
            raise ValueError("intent classifier artifact has no classes")
        if len(self.coef) != len(self.classes):
            raise ValueError("intent classifier coefficients do not match classes")
        if len(self.intercept) != len(self.classes):
            raise ValueError("intent classifier intercepts do not match classes")
        if self.ngram_range[0] <= 0 or self.ngram_range[0] > self.ngram_range[1]:
            raise ValueError("intent classifier ngram_range is invalid")
        feature_count = len(self.idf)
        for row in self.coef:
            if len(row) != feature_count:
                raise ValueError("intent classifier coefficient row has invalid length")
        if self.vocabulary and max(self.vocabulary.values()) >= feature_count:
            raise ValueError("intent classifier vocabulary index exceeds feature vector")


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def classify_intent(
    text: str,
    interaction_mode: str | InteractionMode | None = None,
    *,
    classifier_text: str | None = None,
    model_path: str | Path | None = None,
) -> IntentClassifierResult | None:
    try:
        mode = InteractionMode.parse(interaction_mode)
        keyword_intent = infer_task_intent(text, mode)
    except Exception:
        mode = InteractionMode.AUTO
        keyword_intent = TaskIntent.CHAT

    if mode == InteractionMode.PLAN or keyword_intent == TaskIntent.IMPLEMENT:
        return _keyword_result(keyword_intent)

    classifier = _load_classifier(model_path)
    if classifier is not None:
        try:
            result = classifier.classify(classifier_text or text)
            if result.action != "fallback":
                return result
        except Exception:
            pass

    # Artifact fallback intentionally uses the current input's keyword intent,
    # not the sliding-window classifier text. This preserves state-aware callers
    # that may treat TaskIntent.AMBIGUOUS specially.
    return _keyword_result(keyword_intent)


def reset_intent_classifier_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def char_wb_ngrams(text: str, ngram_range: tuple[int, int] = DEFAULT_NGRAM_RANGE) -> list[str]:
    normalized = text.lower()
    grams: list[str] = []
    for word in normalized.split():
        padded = f" {word} "
        for size in range(ngram_range[0], ngram_range[1] + 1):
            for start in range(max(0, len(padded) - size + 1)):
                grams.append(padded[start : start + size])
    return grams


def _load_classifier(model_path: str | Path | None = None) -> ArtifactClassifier | None:
    try:
        location = _locate_model(model_path)
    except Exception:
        return None

    with _CACHE_LOCK:
        cached = _CACHE.get(location.key)
        if cached and cached.signature == location.signature:
            return cached.classifier

    try:
        if location.content is not None:
            content = location.content
        elif location.path is not None:
            content = location.path.read_bytes()
        else:
            return None
        artifact = json.loads(content.decode("utf-8"))
        classifier = ArtifactClassifier(artifact)
    except Exception:
        return None

    with _CACHE_LOCK:
        _CACHE[location.key] = _CacheEntry(signature=location.signature, classifier=classifier)
        while len(_CACHE) > _MAX_CACHE_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
    return classifier


def _keyword_result(intent: TaskIntent) -> IntentClassifierResult:
    return IntentClassifierResult(
        intent=intent,
        confidence=1.0,
        source="keyword_classifier",
        action="accept",
    )


def _locate_model(model_path: str | Path | None) -> _ModelLocation:
    if model_path is not None:
        path = Path(model_path)
        stat = path.stat()
        return _ModelLocation(
            key=str(path.resolve()),
            signature=(str(path.resolve()), stat.st_mtime_ns, stat.st_size),
            path=path,
        )

    resource = resources.files("voidx.data").joinpath(DEFAULT_MODEL_RESOURCE)
    if isinstance(resource, Path):
        stat = resource.stat()
        return _ModelLocation(
            key=f"package:voidx.data/{DEFAULT_MODEL_RESOURCE}",
            signature=(str(resource.resolve()), stat.st_mtime_ns, stat.st_size),
            path=resource,
        )
    content = resource.read_bytes()
    return _ModelLocation(
        key=f"package:voidx.data/{DEFAULT_MODEL_RESOURCE}",
        signature=("embedded", hashlib.sha256(content).hexdigest()[:16]),
        content=content,
    )


__all__ = [
    "ArtifactClassifier",
    "IntentClassifierResult",
    "char_wb_ngrams",
    "classify_intent",
    "reset_intent_classifier_cache",
]
