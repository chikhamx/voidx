# Goal Resolver Robustness Improvements

> **Status: Done**

## Problem

When a user sends a short continuation command like "改" during an active TDD workflow, the goal resolver classifies it as `GENERAL` intent. `update_after_turn` then clears `workflow_runs`, `workflow_route`, and `current_goal`, causing the active workflow node (e.g. tdd status bar) to disappear.

The root fix (GENERAL intent preserves active workflows) is already implemented in `task_state.py`. This spec covers the remaining issues that make the resolver itself more robust.

## Issues

### 1. `_is_vague_continuation` is effectively dead code

**File**: `src/voidx/agent/goal_resolver.py:191`

`_normalize_resolution` returns early at line 176 when `resolution.intent.type == TaskIntent.GENERAL`. The `_is_vague_continuation` check at line 191 is only reached for `CODING` intent, but vague continuations like "继续" are classified as `GENERAL` by the LLM — so the check never fires for the cases it was designed to handle.

**Fix direction**: Move the vague-continuation check before the GENERAL early-return. If the user text is a vague continuation and there is an active workflow, override GENERAL → CODING and preserve the current join.

### 2. Prompt rule 86 is too strict

**File**: `src/voidx/agent/goal_resolver.py:86`

Current rule:
> "If intent does not clearly match any join value, set goal=null and plan=null"

This forces the LLM to return `goal=null` for short continuation commands that don't explicitly name a workflow node. The LLM has no instruction to consider the current active workflow as a valid continuation target.

**Fix direction**: Add a rule like:
> "If the user's message is a short continuation (e.g. 'ok', 'continue', '改', 'go on') and there is an active workflow, set intent=coding, keep the current goal, and set plan.join to the active workflow name."

### 3. `_IMPLEMENTATION_SIGNALS` and `_VAGUE_CONTINUATIONS` lack common Chinese short commands

**File**: `src/voidx/agent/goal_resolver.py:218-248`

Missing entries:
- `_IMPLEMENTATION_SIGNALS`: "改", "做", "写", "加", "删"
- `_VAGUE_CONTINUATIONS`: "改", "做", "好", "对", "嗯嗯", "继续改", "继续做"

**Fix direction**: Add these entries to both tuples/sets.

### 4. Resolver context is too thin

**File**: `src/voidx/agent/goal_resolver.py:62-70`, `src/voidx/runtime/task_state.py:16`

`_INTENT_WINDOW_SIZE = 2` means the resolver only sees 1 previous exchange + the current message. For a conversation like:

1. User: "实现一个 edit tool"
2. Assistant: (long implementation response)
3. User: "改"

The resolver only sees exchange 2 + "改", with no memory of the original coding intent from exchange 1.

**Fix direction**: Increase `_INTENT_WINDOW_SIZE` to 4 (3 previous exchanges + current). This gives the resolver enough context to recognize that "改" continues a coding task.

## Implementation Order

1. Add missing Chinese entries to `_IMPLEMENTATION_SIGNALS` and `_VAGUE_CONTINUATIONS` (trivial)
2. Move vague-continuation check before GENERAL early-return (small logic change)
3. Update prompt rule 86 to instruct LLM about active workflow continuations (prompt edit)
4. Increase `_INTENT_WINDOW_SIZE` from 2 to 4 (constant change + test updates)

## Test Plan

- Unit test: `_is_vague_continuation("改")` returns `True`
- Unit test: `_has_implementation_signals("改")` returns `True`
- Integration test: resolve "改" with active tdd workflow → CODING intent, join=tdd
- Integration test: resolve "改" with no active workflow → GENERAL intent (fallback)
- Update `test_intent_window_text` if `_INTENT_WINDOW_SIZE` changes
