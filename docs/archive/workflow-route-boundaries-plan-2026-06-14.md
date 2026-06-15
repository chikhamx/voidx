# Workflow Route Boundaries Implementation Plan

> **Status: Done**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `next_workflow` as the primary routing contract with `workflow_start` and `workflow_end`, preserving `Goal` as the semantic layer.

**Architecture:** Add route fields to goal/task state, normalize resolver output into a persisted `WorkflowRoute`, use `workflow_start` for turn reconciliation, and enforce `workflow_end` during graph auto-advance. Do not keep a `next_workflow` compatibility fallback.

**Tech Stack:** Python, Pydantic models, pytest, existing voidx workflow DAG/runtime.

---

### Task 1: Goal Resolver Route Schema

**Files:**
- Modify: `src/voidx/runtime/task_state.py`
- Modify: `src/voidx/agent/goal_resolver.py`
- Test: `tests/test_agent/test_goal_resolver.py`

- [x] Add failing tests for review-only and review-and-fix route fields.
- [x] Add `workflow_start`, `workflow_end`, and `WorkflowRoute`.
- [x] Normalize route names and remove `next_workflow` compatibility.
- [x] Run `tests/test_agent/test_goal_resolver.py -v`.

### Task 2: Turn Reconciliation

**Files:**
- Modify: `src/voidx/workflow/reconcile.py`
- Modify: `src/voidx/agent/graph/turn_runner.py`
- Test: `tests/test_workflow_reconcile.py`
- Test: `tests/test_agent/test_run_loop.py`

- [x] Add failing tests for `workflow_start` activating the initial node.
- [x] Route stale precursor override through `workflow_start`.
- [x] Persist `workflow_route` in `TaskState`.
- [x] Run workflow reconcile and run-loop focused tests.

### Task 3: Auto-Advance Boundary

**Files:**
- Modify: `src/voidx/agent/graph/tool_executor.py`
- Test: `tests/test_agent/test_core_flow.py`
- Test: `tests/test_tools/test_basic.py`

- [x] Add failing tests for `review -> review` stopping without feedback.
- [x] Add failing tests for `review -> verify` continuing into feedback.
- [x] Enforce route boundary during auto-advance state updates.
- [x] Run graph core and tool state focused tests.

### Task 4: Runtime Context And Regression

**Files:**
- Modify: `src/voidx/agent/runtime_context.py`
- Test: `tests/test_agent/test_runtime_context.py`

- [x] Render workflow route in Current Task State.
- [x] Run focused workflow, goal resolver, core-flow, and runtime context tests.
