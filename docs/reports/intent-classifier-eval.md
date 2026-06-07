# Intent Classifier Evaluation Report

Date: 2026-06-07T16:50:31.068995+00:00

## Summary

| Metric | Value | Gate | Passed |
|--------|-------|------|--------|
| Accuracy | 0.9843 | informational | yes |
| Macro F1 | 0.9848 | >= 0.85 | yes |
| Implement false positives | 0 | <= 0 | yes |
| Inspect/Design recall | 0.9769 | >= 0.9 | yes |
| Average inference latency (ms) | 0.0226 | <= 1.0 | yes |
| p95 inference latency (ms) | 0.0477 | <= 1.0 | yes |
| Model artifact size (KB) | 1381.0 | <= 1536.0 | yes |

## Inputs

- Train data: `data/intent/train.jsonl` (1784 rows)
- Eval data: `data/intent/eval.jsonl` (446 rows)
- Model artifact: `src/voidx/data/intent_classifier.json`
- Training model: character n-gram TF-IDF logistic regression
- Evaluation path: exported JSON artifact with pure-Python inference

## Per-Label Metrics

| Intent | Precision | Recall | F1 | Support |
|--------|-----------|--------|----|---------|
| chat | 1.0 | 0.9722 | 0.9859 | 72 |
| inspect | 0.9841 | 0.9538 | 0.9688 | 65 |
| design | 1.0 | 1.0 | 1.0 | 46 |
| review | 0.9625 | 1.0 | 0.9809 | 77 |
| implement | 1.0 | 0.9859 | 0.9929 | 71 |
| debug | 0.9655 | 0.9825 | 0.9739 | 57 |
| ambiguous | 0.9831 | 1.0 | 0.9915 | 58 |

## Confusion Matrix

| Actual \ Predicted | chat | inspect | design | review | implement | debug | ambiguous |
|---|---|---|---|---|---|---|---|
| chat | 70 | 0 | 0 | 0 | 0 | 1 | 1 |
| inspect | 0 | 62 | 0 | 3 | 0 | 0 | 0 |
| design | 0 | 0 | 46 | 0 | 0 | 0 | 0 |
| review | 0 | 0 | 0 | 77 | 0 | 0 | 0 |
| implement | 0 | 0 | 0 | 0 | 70 | 1 | 0 |
| debug | 0 | 1 | 0 | 0 | 0 | 56 | 0 |
| ambiguous | 0 | 0 | 0 | 0 | 0 | 0 | 58 |

## Gate Result

Passed.
