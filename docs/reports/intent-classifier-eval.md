# Intent Classifier Evaluation Report

Date: 2026-06-12T10:52:41.689016+00:00

## Summary

| Metric | Value | Gate | Passed |
|--------|-------|------|--------|
| Accuracy | 0.9933 | informational | yes |
| Macro F1 | 0.9918 | >= 0.85 | yes |
| Coding recall | 1.0 | >= 0.9 | yes |
| General precision | 1.0 | >= 0.8 | yes |
| Average inference latency (ms) | 0.0184 | <= 1.0 | yes |
| p95 inference latency (ms) | 0.0386 | <= 1.0 | yes |
| Model artifact size (KB) | 573.7 | <= 1536.0 | yes |

## Inputs

- Train data: `data/intent/train.jsonl` (1784 rows)
- Eval data: `data/intent/eval.jsonl` (446 rows)
- Model artifact: `src/voidx/data/intent_classifier.json`
- Training model: character n-gram TF-IDF logistic regression
- Evaluation path: exported JSON artifact with pure-Python inference

## Per-Label Metrics

| Intent | Precision | Recall | F1 | Support |
|--------|-----------|--------|----|---------|
| coding | 0.9906 | 1.0 | 0.9953 | 316 |
| general | 1.0 | 0.9769 | 0.9883 | 130 |

## Confusion Matrix

| Actual \ Predicted | coding | general |
|---|---|---|
| coding | 316 | 0 |
| general | 3 | 127 |

## Gate Result

Passed.
