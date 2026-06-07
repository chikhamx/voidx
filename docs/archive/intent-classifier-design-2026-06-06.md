# Intent Classifier Design

> **Status: Done**

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

## Runtime Path

The classifier does not replace the whole intent pipeline. State-aware hard
rules stay in `resolve_turn_intent()`. The local classifier module combines the
replaceable JSON model with the existing keyword matcher:

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
  +-- intent_classifier.classify_intent(text, classifier_text=window)
  |     |
  |     +-- keyword says explicit implement? -> accept existing keyword behavior
  |     |
  |     +-- JSON model available? -> classify recent N=2 user turns
  |     +-- high-confidence safe intent -> accept
  |     +-- implement prediction -> suggest only, do not grant write intent
  |     +-- low confidence / invalid model -> keyword fallback
```

The LLM `on_intent` tool remains the runtime-owned refinement path. The local
classifier only improves the initial task state shown to the model.

`intent_classifier.json` is intentionally replaceable. The runtime loader
observes the artifact path/signature and reloads the classifier when the JSON is
replaced, so retraining can update the model without code changes.

The JSON model input uses a sliding window of the most recent two user turns:

```text
previous_user_text [SEP] current_user_text
```

When no previous user turn exists, the model sees only the current text. This
window is used only for the ML model. State-aware hard rules and keyword
fallback continue to inspect the current input only, so an earlier
implementation request cannot contaminate a later "好了" or "可以" turn.

## Safety Rules

### Implement Intent Does Not Auto-Escalate

The local classifier must not grant implementation/write authority by itself.

- Existing hard rules may still return `IMPLEMENT`:
  - direct implementation commands;
  - approval-only phrases when there is a pending implementation approval.
- Existing keyword matching may still return `IMPLEMENT` for explicit
  implementation phrases, preserving pre-classifier behavior.
- A classifier prediction of `implement` is recorded as a suggestion unless the
  request also matches hard direct-implementation rules or the keyword matcher's
  explicit implementation path.
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

The 2026-06-07 research run in `research/intent-classifier/` compared FastText
with character n-gram TF-IDF logistic regression. The result supports the
original pure-Python-runtime direction:

| Candidate | Data size | Macro F1 | Implement FP | Inspect/Design Recall | Latency | Size |
|-----------|-----------|----------|---------------|------------------------|---------|------|
| LR JSON artifact | 2000 | 0.9848 | 0 | 0.9769 | avg 0.0226 ms / p95 0.0477 ms | 1381.0 KB JSON |
| LR research baseline | 5000 | 0.9991 | 0 | 1.0000 | avg 0.0118 ms / p95 0.0120 ms | 812.7 KB pickle |
| FastText | 5000 | 0.8554 | 0 | 0.8602 | avg 0.0014 ms | 19.4 MB |

FastText is rejected for V1 because it exceeds the 1.5 MB artifact gate and
performs worse on Chinese text without explicit segmentation.
The checked-in Phase A report uses true inspect/design recall; the research
report's summary table used the same column name for an inspect/design F1
aggregate.

Recommended V1:

- character n-gram TF-IDF logistic regression trained from the size-2000 split;
- model artifact as JSON under `src/voidx/data/`;
- no required runtime dependency beyond the standard library;
- `importlib.resources` for loading.

The training script may use scikit-learn in the developer environment, but the
published artifact must not be a pickle and runtime classification must not
import scikit-learn, numpy, or FastText. The script evaluates the exported JSON
artifact with the same pure-Python inference path intended for runtime use.

Multinomial naive Bayes remains an acceptable fallback if future LR exports
cannot meet the size or latency gate. FastText can be revisited only if the
package-size constraint changes.

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

Phase A uses the research size-2000 split as the first checked-in dataset:

```text
data/intent/train.jsonl  # 1784 rows
data/intent/eval.jsonl   # 446 rows
```

The data is synthetic plus near-miss and multi-turn snippets. It is sufficient
to pass the V1 offline gate, but it is not a substitute for future evaluation on
real anonymized user turns.

## Training Script

Add:

```text
scripts/train_intent_classifier.py
```

Responsibilities:

1. Load `train.jsonl` and `eval.jsonl`.
2. Train the classifier.
3. Export the classifier to the runtime-safe JSON artifact:

   ```text
   src/voidx/data/intent_classifier.json
   ```

4. Evaluate the exported artifact, not the in-memory training model.
5. Print and write an evaluation report:

   ```text
   docs/reports/intent-classifier-eval.md
   ```

6. Exit non-zero if acceptance thresholds fail.

The JSON artifact stores:

- schema version and model type;
- label order;
- character n-gram settings;
- accept/suggest decision thresholds;
- vocabulary and IDF vector;
- classifier coefficients and intercepts;
- gate thresholds and evaluation metrics.

Do not store or load pickle artifacts in `src/voidx/data/`.

## Delivery Phases

### Phase A: Training And Evaluation Only

Phase A produces:

- labeled train/eval data;
- training script;
- runtime-safe JSON model artifact;
- evaluation report;
- tests for artifact size, metrics, latency, and gate failure behavior.

Phase A must not modify `resolve_turn_intent()` or any runtime behavior.
It may add package data and a `voidx.data` package so the artifact will be
available to Phase B.

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
| p95 inference latency | <= 1 ms on local dev machine |
| Macro F1 | >= 0.85 |
| `implement` false positives | 0 on eval set |
| `inspect/design` recall | >= 0.90 |

If these thresholds do not pass, stop after training/evaluation and do not wire
the classifier into `resolve_turn_intent()`.

## Runtime API

Add a small runtime module in Phase B after the Phase A gate passes:

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

def classify_intent(
    text: str,
    interaction_mode: str | InteractionMode | None = None,
    *,
    classifier_text: str | None = None,
    model_path: str | Path | None = None,
) -> IntentClassifierResult | None:
    ...
```

Behavior:

- combines the replaceable JSON artifact with `infer_task_intent()`;
- uses keyword `implement` as a compatibility guard before accepting a safe ML
  prediction;
- accepts an optional `classifier_text` built from the recent N=2 user-turn
  window;
- falls back to keyword matching if the model is unavailable, invalid, or low
  confidence;
- never raises during normal runtime classification;
- caches up to eight loaded models in memory;
- reloads the model if the JSON artifact changes on disk;
- hashes embedded resource bytes for cache signatures so same-size replacements
  are still detected;
- normalizes text consistently with the training script;
- returns `suggest` rather than `accept` for classifier-predicted `implement`.

## Runtime Integration

Modify `resolve_turn_intent()`:

1. Run existing hard rules first:
   - plan mode;
   - approval-only phrase;
   - direct implementation command.
2. Build `classifier_text` from `TaskState.recent_user_texts[-1]` plus the
   current input, separated by `[SEP]`.
3. Call `classify_intent(text, interaction_mode, classifier_text=classifier_text)`.
4. If classifier returns `accept`, return that intent with a source-aware reason
   such as `local classifier matched <intent>` or
   `keyword classifier matched <intent>`.
5. If classifier returns `suggest`, keep the runtime safe:
   - for `implement`, return `DESIGN` or `AMBIGUOUS`;
   - include a reason telling the model to use `on_intent` / `clarify` before
     editing.
6. If classifier returns `fallback` or `None`, use existing `infer_task_intent()`
   as a final defensive fallback.

The runtime context should expose classifier metadata only when useful, for
example:

```text
Intent resolution: local classifier suggested implement confidence=0.88; confirmation required
```

## Packaging

Phase A creates `src/voidx/data/` and updates `pyproject.toml` package data:

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
| `test_resolve_turn_intent_keeps_design_behavior_after_classifier_integration` | Runtime integration preserves design intent behavior |
| `test_package_data_includes_intent_classifier` | Wheel package data includes the JSON artifact |

Runtime tests, only after the model passes evaluation:

| Test | Description |
|------|-------------|
| `test_classifier_high_confidence_safe_intent` | Safe high-confidence intent returns directly |
| `test_classifier_missing_model_falls_back` | Missing model keeps old rule-based behavior |
| `test_classifier_low_confidence_uses_keyword_fallback` | Low-confidence model output falls back to keyword matching |
| `test_classifier_window_text_does_not_pollute_keyword_fallback` | Keyword fallback uses current text, not window text |
| `test_classifier_reload_after_model_replacement` | Replacing JSON model changes future classification without code changes |
| `test_embedded_model_signature_uses_content_hash` | Embedded model cache signatures change for same-size content replacements |
| `test_classifier_cache_is_bounded` | Model cache keeps a small bounded number of entries |
| `test_classifier_implement_prediction_is_suggest_only` | ML implement does not directly grant implement intent |
| `test_keyword_implement_preserves_existing_behavior` | Explicit keyword implement still grants implementation intent |
| `test_keyword_intent_avoids_broad_problem_debug_match` | Keyword fallback does not treat generic "问题" as debug |
| `test_keyword_intent_uses_word_boundaries_for_short_english_hints` | Short English hints do not match inside unrelated words |
| `test_plan_mode_ignores_classifier` | Plan mode still forces design |
| `test_approval_phrase_without_pending_plan_stays_ambiguous` | Approval text remains state-aware |
| `test_chinese_and_mixed_input_classification` | Chinese and mixed-language examples classify correctly |
| `test_intent_classifier_uses_recent_two_turn_window_for_short_input` | N=2 context disambiguates short current input |
| `test_intent_window_keeps_only_two_recent_user_inputs` | Runtime state stores at most two recent user turns |
| `test_intent_window_does_not_override_approval_without_pending_plan` | Approval-only hard rule ignores window context |
| `test_intent_window_does_not_override_direct_short_command` | Direct short command hard rule ignores window context |

## Acceptance Criteria

- A trained JSON model artifact exists and is included as package data.
- An evaluation report documents all required metrics.
- The evaluation gate passes before runtime integration.
- Runtime integration uses the JSON model after hard safety rules.
- Classifier failures and low-confidence model predictions fall back to keyword
  matching.
- Replacing `intent_classifier.json` reloads future classifications without code
  changes.
- Classifier output never bypasses plan mode, pending approval, or direct
  implementation safety rules.
- `implement` false positives are blocked by design and by tests.
