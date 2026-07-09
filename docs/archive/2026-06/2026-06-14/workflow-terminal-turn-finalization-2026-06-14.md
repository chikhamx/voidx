# Workflow Terminal Turn Finalization Design

> **Status: Done**
> Date: 2026-06-14

## Background

`advance_workflow(condition="done")` marks an active workflow node as
`satisfied` and does not activate a successor. This correctly represents a
terminal workflow exit, but the LangGraph topology still routes every
`execute_tools` result back to `call_llm`.

The visible effect is an unnecessary extra model call after the workflow is
already done. In a real archive flow, the agent performed the archive work,
called `advance_workflow("done")` twice to close active workflow nodes, then
called the LLM again only to say "归档完毕".

## Goals

- Skip the next LLM call when successful terminal workflow exits leave no active
  workflow nodes.
- Preserve the current tool batch semantics, including multiple
  `advance_workflow(done)` calls in one assistant message.
- Preserve non-terminal transitions such as `implemented -> verify`; those must
  still route back to the LLM so the newly active workflow node can run.
- Keep the behavior observable through graph state rather than prompt wording.

## Non-Goals

- Do not change `advance_workflow` validation, evidence requirements, or DAG
  transition behavior.
- Do not stop immediately after the first `advance_workflow(done)` in a batch.
- Do not suppress normal final answers after ordinary tools.
- Do not introduce a new workflow status or graph node.

## Design

`execute_tools` should complete the whole approved tool batch as it does today.
After state patches and auto-advance events are applied, it can determine whether
the batch closed the current workflow:

1. At least one successful executed tool is `advance_workflow`.
2. Its `workflow_transition.condition` is the terminal workflow condition
   (`done`).
3. The final merged workflow runs contain no active workflow nodes.

When all three are true, `execute_tools` returns `should_continue=False`. The
graph topology will route `execute_tools` to `finalize` instead of `call_llm`.

This uses the existing `AgentState.should_continue` field as the routing signal.
The graph topology replaces the unconditional `execute_tools -> call_llm` edge
with a conditional route:

```text
execute_tools -- should_continue=False --> finalize
execute_tools -- otherwise -------------> call_llm
```

## Edge Cases

- Multiple active workflows: `advance_workflow(done)` already requires an
  explicit `workflow` target when ambiguous. If the assistant closes multiple
  active workflows in one batch, the executor waits until the batch is complete
  before checking whether any active workflow remains.
- Terminal close plus ordinary tool calls: the whole batch still runs. The turn
  only stops after successful execution and state merging.
- Terminal close with successor activation: impossible for `done` under current
  DAG policy, but the final "no active workflows" check protects the route.
- Failed or denied `advance_workflow`: does not stop the turn; the tool result
  should still be returned to the model for correction.

## Implementation Plan

1. Add regression tests in `tests/test_agent/test_core_flow.py`:
   - terminal `advance_workflow(done)` returns `should_continue=False`;
   - non-terminal `advance_workflow(implemented)` keeps continuation enabled.
2. Add a topology unit test showing the execute-tools router sends
   `should_continue=False` to `finalize`.
3. Implement a small helper in `tool_executor.py` that detects successful
   terminal workflow completion from executed tool metadata and final workflow
   state.
4. Add an execute-tools router in `topology.py` and wire the conditional graph
   edge.
5. Run focused tests for graph/tool workflow behavior.

## Test Matrix

| Scenario | Expected |
| --- | --- |
| Single active `verify`, `advance_workflow(done)` succeeds | `should_continue=False`, workflow is `satisfied` |
| `tdd`, `advance_workflow(implemented)` activates `verify` | no terminal stop, `verify` remains active |
| Multiple `advance_workflow(done)` calls close all active workflows | batch completes, then stops before next LLM |
| Failed `advance_workflow(done)` | no stop signal, model can correct the call |
