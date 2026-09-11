# Task-state history cache experiment (superseded)

## Status and purpose

The experimental switch design is superseded by the approved [Task State on-demand update and periodic reminder policy](../specs/task-state-reminder-policy-2026-09-10.md). That specification is the source of truth for triggers, thresholds, lifecycle handling, and acceptance criteria. This document preserves the experiment's rationale and explains how to compare the old behavior with the new policy; it does not certify that runtime integration or cache improvements have been verified.

Task State is a runtime snapshot of an agent's goal and execution state, distinct from a real user instruction. The earlier retention experiment kept previously sent snapshots and appended a complete snapshot on every model call. It explored whether avoiding deletion of old state messages could improve prompt-prefix reuse, but repeated unchanged snapshots increased context usage.

## Replacement policy

The approved policy retains sent snapshots and appends a new one only when needed: initially, after a history reset, for new real user input, for a substantive state change, or for a periodic reminder. It replaces the experimental switches with built-in behavior for agents whose own prompt policy enables Current Task State. Chat profiles that suppress that section remain exempt; each main agent and subagent is evaluated independently.

Historical Task State entries describe their state at the time; the last snapshot is the latest known state. Newer real user instructions and tool facts remain valid. A snapshot cannot elevate its instruction priority or override newer user requests. This interpretation belongs once in the shared stable prompt, not in every snapshot.

Under the approved lifecycle, preparing or retrying a request does not commit a snapshot or consume a reminder interval. Only an accepted successful logical model request commits its state. Compaction or invalid history anchors establish a new baseline rather than restoring obsolete snapshots. Main agents and individual subagent runs keep separate snapshot histories and reminder baselines. See the specification for the complete rules and required integration tests.

## Comparing behavior

Use test fixtures or two recorded code versions to compare the earlier **append on every call** behavior with the approved **on-demand updates and periodic reminders** policy. Do not add or rely on production command switches for this comparison.

1. Record the exact fixture or version for each side, plus the provider, model, reasoning effort, and task inputs. Use independent fresh sessions with matching settings.
2. Exercise the same multi-tool task in each version, separately for the main agent and subagents. Include unchanged-state tool loops, substantive state changes, and new real user input. In fixtures, also exercise retries and history resets.
3. Record snapshot counts and tokens, total input tokens, cache-read tokens within tool loops, request times, and any compaction. Note differences in system instructions, tool definitions, or other request-prefix content that could confound the comparison.
4. Check task outcomes for omissions, repeated work, and actions based on stale state. Reduced snapshot repetition alone does not establish task quality.
5. Report the tested versions, commands, environment, and results. Distinguish deterministic fixture assertions from observed provider cache behavior, and identify any main-agent or subagent integration paths not tested.

Fewer redundant snapshots is a deterministic acceptance target. Cache reuse still depends on provider routing, protocol behavior, and other prefix changes; neither retention nor the new policy guarantees cache hits. Cache improvement and task quality require measurement. Passing prompt or contract tests validates prompt content, not runtime lifecycle integration or upstream caching.
