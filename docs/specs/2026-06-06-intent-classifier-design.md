# Intent Classifier Design

> **Status: Draft**

Date: 2026-06-06

## Problem

voidx currently starts each turn with a lightweight intent guess from
`resolve_turn_intent()`. That guess is rule-based:

1. interaction mode hard rules, such as plan mode forcing design;
2. approval-only phrases, such as "可以" confirming pending implementation;
3. short direct implementation commands, such as "fix" or "改吧";
4. keyword matching through `infer_task_intent()`;
5. fallback to `chat`.

The LLM can later refine the intent by calling `on_intent`, but that call costs
latency and tokens. We want a cheap local classifier to improve the initial
intent guess and reduce unnecessary `on_intent` calls without weakening
permission or implementation safety.

## Non-Negotiable Gate

Do not wire a classifier into runtime until a trained model and offline
evaluation report exist.

Implementation order:

1. Build a labeled dataset and training script.
2. Train a local classifier artifact.
3. Run offline evaluation and record the metrics.
4. Only if the evaluation passes the acceptance thresholds, add runtime
   integration.

If the trained model is missing, invalid, or fails to load, voidx must silently
fall back to the current rule-based behavior.

## Current Runtime Path

The classifier does not replace the whole intent pipeline. It fits inside
`resolve_turn_intent()` after hard safety rules and before the old keyword
fallback:

```text
User text
  |
  v
resolve_turn_intent()
  |
  +-- plan mode? -> DESIGN
  +-- approval-only phrase? -> IMPLEMENT only if pending approval exists
  +-- direct implementation command? -> IMPLEMENT
  |
  +-- local classifier available?
  |     +-- high-confidence safe intent -> accept
  |     +-- implement prediction -> suggest only, do not grant write intent
  |     +-- low confidence -> fallback
  |
  +-- existing infer_task_intent()
```

The LLM `on_intent` tool remains the runtime-owned refinement path. The local
classifier only improves the initial task state shown to the model.

## Safety Rules

### Implement Intent Does Not Auto-Escalate

The local classifier must not grant implementation/write authority by itself.

- Existing hard rules may still return `IMPLEMENT`:
  - direct implementation commands;
  - approval-only phrases when there is a pending implementation approval.
- A classifier prediction of `implement` is recorded as a suggestion unless the
  request also matches the hard direct-implementation rules.
- Suggested implement intent should produce an initial `DESIGN` or `AMBIGUOUS`
  state with a reason that tells the LLM to call `on_intent` or `clarify`
  before editing.

This preserves the existing `refine_intent()` behavior where low-confidence or
unsafe implementation requests require confirmation.

### Plan Mode Wins

Plan mode always returns `DESIGN`, regardless of classifier output.

### Approval Phrases Stay State-Aware

Approval-only phrases still depend on `TaskState.pending_approval`. A classifier
must not treat "可以" / "ok" as implementation when there is no pending plan to
approve.

## Model Choice

The original proposal used FastText. That is still possible, but it introduces a
native dependency and wheel packaging risk. voidx should prefer a pure-Python
classifier unless FastText packaging is explicitly accepted.

Recommended V1:

- character n-gram logistic regression or multinomial naive Bayes;
- model artifact as JSON or compact binary under `src/voidx/data/`;
- no required runtime dependency beyond the standard library;
- `importlib.resources` for loading.

FastText can be revisited later if the pure-Python model fails evaluation.

## Dataset

Add versioned training data:

```text
data/intent/train.jsonl
data/intent/eval.jsonl
```

Each JSONL row:

```json
{"text": "看看这个设计文档", "intent": "inspect", "source": "handwritten", "notes": ""}
```

Labels match `TaskIntent`:

- `chat`
- `inspect`
- `design`
- `review`
- `debug`
- `implement`
- `ambiguous`

Dataset requirements:

- include Chinese, English, and mixed-language examples;
- include approval-only phrases with and without pending-approval context;
- include near misses such as "看看这个 bug" vs "修复这个 bug";
- intentionally over-sample `inspect`, `design`, and `ambiguous` phrases that
  are commonly mistaken as implementation requests;
- keep train/eval split stable so metrics are comparable across changes.

## Training Script

Add:

```text
scripts/train_intent_classifier.py
```

Responsibilities:

1. Load `train.jsonl` and `eval.jsonl`.
2. Train the classifier.
3. Write the model artifact, for example:

   ```text
   src/voidx/data/intent_classifier.json
   ```

4. Print and optionally write an evaluation report:

   ```text
   docs/reports/intent-classifier-eval.md
   ```

5. Exit non-zero if acceptance thresholds fail.

## Delivery Phases

### Phase A: Training And Evaluation Only

Phase A produces:

- labeled train/eval data;
- training script;
- model artifact;
- evaluation report;
- tests for artifact size, metrics, latency, and gate failure behavior.

Phase A must not modify `resolve_turn_intent()` or any runtime behavior.

### Phase B: Runtime Integration

Phase B starts only after Phase A passes the evaluation gate. It adds the
runtime loader and integrates classifier output into `resolve_turn_intent()`
under the safety rules below.

## Evaluation Gate

The training script must report:

- overall accuracy;
- macro F1;
- per-label precision / recall / F1;
- confusion matrix;
- model artifact size;
- average and p95 single-input inference latency;
- number of `implement` false positives.

Minimum acceptance thresholds for runtime integration:

| Metric | Required |
|--------|----------|
| Model size | <= 1.5 MB |
| Average inference latency | <= 1 ms on local dev machine |
| Macro F1 | >= 0.85 |
| `implement` false positives | 0 on eval set |
| `inspect/design` recall | >= 0.90 |

If these thresholds do not pass, stop after training/evaluation and do not wire
the classifier into `resolve_turn_intent()`.

## Runtime API

Add a small runtime module only after the evaluation gate passes:

```text
src/voidx/runtime/intent_classifier.py
```

Public API:

```python
class IntentClassifierResult(BaseModel):
    intent: TaskIntent
    confidence: float
    source: str = "local_classifier"
    action: Literal["accept", "suggest", "fallback"]

def classify_intent(text: str) -> IntentClassifierResult | None:
    ...
```

Behavior:

- returns `None` if the model is unavailable or invalid;
- never raises during normal runtime classification;
- caches the loaded model in memory;
- normalizes text consistently with the training script;
- returns `suggest` rather than `accept` for classifier-predicted `implement`.

## Runtime Integration

Modify `resolve_turn_intent()`:

1. Run existing hard rules first:
   - plan mode;
   - approval-only phrase;
   - direct implementation command.
2. Call `classify_intent(text)` if available.
3. If classifier returns `accept` for a safe non-implement intent, return that
   intent with reason `local classifier matched <intent>`.
4. If classifier returns `suggest`, keep the runtime safe:
   - for `implement`, return `DESIGN` or `AMBIGUOUS`;
   - include a reason telling the model to use `on_intent` / `clarify` before
     editing.
5. If classifier returns `fallback` or `None`, use existing `infer_task_intent()`.

The runtime context should expose classifier metadata only when useful, for
example:

```text
Intent resolution: local classifier suggested implement confidence=0.88; confirmation required
```

## Packaging

If the model artifact is placed under `src/voidx/data/`, update
`pyproject.toml` package data:

```toml
[tool.setuptools.package-data]
"voidx.data" = ["intent_classifier.json"]
```

The `voidx.data` package must contain `__init__.py` so `importlib.resources`
can load the artifact from wheels.

## Tests

Training/evaluation tests:

| Test | Description |
|------|-------------|
| `test_training_script_writes_model` | Training creates the model artifact |
| `test_eval_report_contains_required_metrics` | Evaluation report includes all gate metrics |
| `test_eval_gate_rejects_implement_false_positive` | Gate fails if eval has implement false positives |
| `test_model_artifact_size` | Artifact is <= 1.5 MB |
| `test_classifier_latency` | Average inference <= 1 ms |

Runtime tests, only after the model passes evaluation:

| Test | Description |
|------|-------------|
| `test_classifier_high_confidence_safe_intent` | Safe high-confidence intent returns directly |
| `test_classifier_missing_model_falls_back` | Missing model keeps old rule-based behavior |
| `test_classifier_implement_prediction_is_suggest_only` | ML implement does not directly grant implement intent |
| `test_plan_mode_ignores_classifier` | Plan mode still forces design |
| `test_approval_phrase_without_pending_plan_stays_ambiguous` | Approval text remains state-aware |
| `test_chinese_and_mixed_input_classification` | Chinese and mixed-language examples classify correctly |

## Acceptance Criteria

- A trained model artifact exists and is loaded through package resources.
- An evaluation report documents all required metrics.
- The evaluation gate passes before runtime integration.
- Classifier failures fall back to existing behavior.
- Classifier output never bypasses plan mode, pending approval, or direct
  implementation safety rules.
- `implement` false positives are blocked by design and by tests.
