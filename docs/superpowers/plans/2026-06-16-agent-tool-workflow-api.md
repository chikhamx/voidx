# Agent Tool Workflow API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the `agent` tool from persona/max-step delegation to explicit workflow child-task delegation using `goal_resolution` and `result`.

**Architecture:** `AgentTool` owns schema validation and passes a `GoalResolution` plus result contract to the graph runner. The graph runner builds child workflow context and delegates to `run_subagent`, which derives persona and step budget from workflow state instead of accepting them from the model.

**Tech Stack:** Python, Pydantic, LangGraph-style runtime state, pytest.

---

### Task 1: Agent Tool Schema And Validation

**Files:**
- Modify: `src/voidx/tools/agent.py`
- Test: `tests/test_tools/test_basic.py`
- Test: `tests/test_agent/test_core_flow.py`

- [x] **Step 1: Write failing schema tests**

Assert the `agent` schema requires `description`, `goal_resolution`, and `result`, while no longer requiring `persona`, `max_steps`, `delegation_reason`, `expected_output`, or `parent_evidence`.

- [x] **Step 2: Write failing validation tests**

Cover missing `goal_resolution.goal`, missing `plan.join`, missing `plan.leave`, unknown workflow nodes, empty `result.format`, and review goals that do not enter `plan.join=review`.

- [x] **Step 3: Implement minimal schema**

Add `AgentResultContract`, change `AgentInput`, and replace old rejection logic with workflow-route validation.

- [x] **Step 4: Run focused tests**

Run `.venv/bin/python -m pytest tests/test_tools/test_basic.py -v`.

### Task 2: Runner Wiring

**Files:**
- Modify: `src/voidx/tools/agent.py`
- Modify: `src/voidx/agent/graph/core.py`
- Modify: `src/voidx/agent/graph/subagent.py`
- Test: `tests/test_tools/test_basic.py`
- Test: `tests/test_agent/test_core_flow.py`

- [x] **Step 1: Write failing runner tests**

Assert the runner receives `goal_resolution` and `result`, and no model-provided `max_steps` or `persona`.

- [x] **Step 2: Implement runner signatures**

Change `AgentTool.execute`, `Graph._subagent_runner`, and `run_subagent` to accept `goal_resolution` and `result`.

- [x] **Step 3: Build child task state**

Construct child `TaskState` from `goal_resolution`, reconcile workflow runs, derive persona from workflow runs, and choose step budget internally.

- [x] **Step 4: Run focused tests**

Run `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_tools/test_basic.py -v`.

### Task 3: Result Contract Context And Cleanup

**Files:**
- Modify: `src/voidx/agent/graph/subagent.py`
- Modify: tests as needed

- [x] **Step 1: Write failing result contract test**

Assert result contract metadata is passed to the child runtime and reflected in subagent metadata/events where available.

- [x] **Step 2: Add result contract prompt text**

Append concise final-output instructions to the child task description or runtime constraints.

- [x] **Step 3: Remove obsolete helpers**

Remove old persona/step compatibility helpers and review keyword validation once no callers need them.

- [x] **Step 4: Run focused verification**

Run `.venv/bin/python -m pytest tests/test_tools/test_basic.py tests/test_agent/test_core_flow.py tests/test_workflow_reconcile.py -v`.
