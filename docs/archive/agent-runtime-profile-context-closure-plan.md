> **Status: Done** — Archived on 2026-07-25.

---
name: agent-runtime-profile-context-closure-plan
display_name: Agent Runtime Profile Context 闭环执行计划
description: 将 chat/coding/未来 loop 的 per-turn identity、profile、workspace 和 tool policy 收敛成统一上下文，修复 gateway profile 路由、并发串味和 chat 工具边界缺口
doc_type: tasks
audience: llm
status: proposed
source_design: docs/specs/agent-runtime-phase-2-closure-plan.md
---

# Agent Runtime Profile Context Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `AgentRuntime.run_turn()` the single profile-aware turn path for coding, chat, and future loop/goal profiles by carrying identity, profile, workspace, and tool policy in one per-turn execution context.

**Architecture:** Introduce one immutable turn execution context built by application services and carried through `TurnRequest` into `LangGraphTurnEngine`. Bind that context into `ThreadExecutionState` so prompt policy, tool policy, workspace, and thread identity are per-turn/per-thread, not mutable fields on the shared `LangGraphExecution` host.

**Tech Stack:** Python 3.12, Pydantic domain models, dataclasses/contextvars for graph execution state, LangGraph runtime, JSON-RPC gateway, TypeScript SPA.

---

## 1. Context

Phase 2 closed the first runtime contract and added a backend chat path, but profile context is still split across:

- `TurnRequest.profile`
- `TurnRequest.context`
- `ThreadExecutionContext`
- shared host fields (`_active_chat_tool_view`, `_active_profile`)
- gateway `thread_id` parameters
- frontend session state

This creates three concrete gaps:

1. **Gateway submit loses the target thread id** when `GatewayHeadlessFrontend` passes only `context`, so `_route_chat_turn()` receives an empty `thread_id` and chat sessions fall through to coding.
2. **Chat profile/tool view can leak across concurrent turns** because `TurnRunner` stores `_active_chat_tool_view` and `_active_profile` on the shared host.
3. **Chat tool policy is name-only and incomplete** because path extraction misses `file_path`, execution-time denial does not inspect args, and MCP `op="call"` is allowed as a broad gateway call.

This plan fixes those gaps without changing coding behavior.

## 2. Target Runtime Flow

```text
session.submit(thread_id)
  -> AgentService resolves SessionInfo(runtime_profile, workspace)
  -> TurnRouter builds TurnRequest(thread, profile, runtime, context)
  -> AgentRuntime.run_turn(request)
  -> LangGraphTurnEngine.run(..., context=request.context)
  -> TurnRunner binds TurnExecutionContext into ThreadExecutionState
  -> prompt/tool/workspace/session behavior reads current_thread_execution_state()
```

Coding and chat keep the same execution chain. Only the profile policy differs.

## 3. File Structure

### New files

| Path | Responsibility |
|---|---|
| `src/voidx/agent/domain/turn_context.py` | Typed immutable per-turn context: thread id, session id, runtime profile, workspace, prompt policy, tool policy. |
| `src/voidx/agent/domain/tool_policy.py` | Generic tool-policy contract and default coding policy. Chat policy can implement the same interface. |
| `src/voidx/agent/application/turn_router.py` | Resolve session/profile/workspace and build `TurnRequest` for gateway/direct submits. |
| `src/tests/test_agent/test_turn_router.py` | Unit tests for coding/chat request construction and profile selection. |

### Modified files

| Path | Responsibility |
|---|---|
| `src/voidx/agent/runtime/contracts.py` | Make `TurnRequest.context` a `TurnExecutionContext | None` instead of opaque `Any` where possible. |
| `src/voidx/agent/domain/profile.py` | Add profile-owned policy fields or helpers without coupling domain to infrastructure. |
| `src/voidx/agent/domain/chat_policy.py` | Convert `ChatToolView` into the generic tool-policy shape and inspect full tool calls. |
| `src/voidx/agent/application/chat_service.py` | Stop manually building `ThreadExecutionContext(tool_view=...)`; build profile context via shared helper. |
| `src/voidx/agent/application/coding_service.py` | Build coding profile context via the same helper. |
| `src/voidx/agent/application/agent_service.py` | Delegate submit routing to `TurnRouter`; derive thread id from context when needed. |
| `src/voidx/agent/infrastructure/langgraph/runtime/thread_context.py` | Store active profile/tool/workspace policy in `ThreadExecutionState`. |
| `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py` | Bind turn context into thread state; remove shared host `_active_*` writes. |
| `src/voidx/agent/infrastructure/langgraph/execution.py` | Read active profile/tool policy from thread state; remove `_active_chat_tool_view` and `_active_profile` reads. |
| `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py` | Authorize full tool call through current tool policy immediately before execution. |
| `src/voidx/ui/output/types.py` | Keep UI context focused on UI identity, or alias it to the new turn context only where appropriate. |
| `src/voidx/ui/gateway/session/method/sessions.py` | Preserve `runtime_profile` in create/list/switch/submit responses. |
| `src/voidx/ui/protocol/v2/threads.py` | Keep `runtime_profile` as protocol field and ensure schema export includes it. |
| `frontend/src/ui/sidebar.ts` | Add `runtime_profile` to `ThreadInfo`; make empty-session reuse profile-aware. |
| `frontend/src/main.ts` | Add a real chat-profile creation path or explicitly keep “New session” as coding. |
| `frontend/src/rpc/protocol.schema.json` / `frontend/src/rpc/protocol.d.ts` | Regenerate after backend protocol changes. |

## 4. Design Rules

1. `TurnRequest` is the only production input to `AgentRuntime.run_turn()`.
2. `TurnExecutionContext` is immutable and built before the engine starts.
3. No profile-specific mutable flags live on `LangGraphExecution`.
4. Prompt selection, tool binding, authorization, execution-time denial, and workspace scope read the same active profile context.
5. Coding remains the default profile for existing sessions and CLI/TUI submits.
6. Chat denial never escalates to coding approval.
7. MCP call access stays denied in chat until MCP tools have explicit read/write capability metadata.

## 5. Implementation Tasks

### Task 1: Add failing tests for gateway chat routing

**Files:**
- Modify: `src/tests/test_agent/graph/test_run_loop_startup.py`
- Modify: `src/tests/test_ui/gateway/test_gateway_headless_frontend.py`

- [ ] **Step 1: Add a regression test for context-only submit routing**

Add a test where `_handle_user_input()` receives `context=ThreadExecutionContext(thread_id=chat_id, session_id=chat_id)` and no explicit `thread_id`, with a fake chat session returned by `get_session()`.

Expected assertion:

```python
assert captured_chat == [("hello", chat_id)]
assert captured_coding == []
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
./test.py --backend -- src/tests/test_agent/graph/test_run_loop_startup.py -k "chat_routing" -v
```

Expected: FAIL because `_route_chat_turn()` receives an empty `thread_id`.

- [ ] **Step 3: Add a headless frontend coverage test**

Ensure `GatewayHeadlessFrontend.run_headless()` preserves `context.thread_id` all the way to the submitted callback. Existing tests cover this at the frontend layer; keep or extend them only if the new routing test needs a helper.

- [ ] **Step 4: Do not implement the fix yet**

Leave the failure as the red test for Task 4.

### Task 2: Add the profile/tool context contract

**Files:**
- Create: `src/voidx/agent/domain/tool_policy.py`
- Create: `src/voidx/agent/domain/turn_context.py`
- Modify: `src/voidx/agent/runtime/contracts.py`
- Test: `src/tests/test_agent/domain/test_turn_context.py`
- Test: `src/tests/test_agent/runtime/test_contracts.py`

- [ ] **Step 1: Write domain tests for immutable context**

Test:

```python
def test_turn_execution_context_is_immutable_and_profile_scoped():
    ctx = TurnExecutionContext(
        thread_id="chat:s1",
        session_id="s1",
        runtime_profile=CHAT_PROFILE,
        workspace="/tmp/project",
        tool_policy=ChatToolPolicy.for_workspace("/tmp/project"),
    )
    with pytest.raises(Exception):
        ctx.thread_id = "other"
```

- [ ] **Step 2: Add the generic policy contract**

Create a small protocol-like domain model:

```python
class ToolPolicy(Protocol):
    def visible_tool_ids(self, available_tool_ids: Iterable[str]) -> frozenset[str]: ...
    def check_tool_call(self, tool_name: str, args: Mapping[str, object]) -> ToolPolicyDecision: ...
```

Use `ToolPolicyDecision(allowed: bool, reason: str, requests_approval: bool = False)`.

- [ ] **Step 3: Add default coding policy**

`CodingToolPolicy` should allow all tools and defer actual permission decisions to the existing permission service.

- [ ] **Step 4: Add `TurnExecutionContext`**

Fields:

```python
thread_id: str
session_id: str
runtime_profile: RuntimeProfile
workspace: str = ""
tool_policy: ToolPolicy | None = None
```

Keep `arbitrary_types_allowed=True` if Pydantic is used; otherwise use a frozen dataclass.

- [ ] **Step 5: Type `TurnRequest.context`**

Update `TurnRequest.context` to accept the new type while preserving compatibility for tests that still pass `None`.

- [ ] **Step 6: Run focused domain/runtime tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/domain src/tests/test_agent/runtime -v
```

Expected: PASS.

### Task 3: Convert chat policy to full tool-call authorization

**Files:**
- Modify: `src/voidx/agent/domain/chat_policy.py`
- Test: `src/tests/test_agent/test_chat_policy.py`

- [ ] **Step 1: Add failing tests for `file_path` path extraction**

Test that:

```python
policy.check_tool_call("read", {"file_path": "/outside/secret.txt"}).allowed is False
policy.check_tool_call("read", {"file_path": str(workspace / "a.py")}).allowed is True
```

- [ ] **Step 2: Add failing tests for MCP call denial**

Test that:

```python
policy.check_tool_call("mcp", {"op": "list"}).allowed is True
policy.check_tool_call("mcp", {"op": "load", "server": "docs"}).allowed is True
policy.check_tool_call("mcp", {"op": "call", "server": "docs", "tool": "write_page"}).allowed is False
```

- [ ] **Step 3: Implement full argument-aware chat authorization**

Extract path candidates from:

- `file_path`
- `path`
- `file`
- `directory`
- `paths`
- `moves[].src`
- `moves[].dest`

Chat read-only policy should only allow local tools that are explicitly read-only and in-scope.

- [ ] **Step 4: Preserve no-approval invariant**

Every denied chat decision must return `requests_approval=False`.

- [ ] **Step 5: Run focused policy tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_chat_policy.py -v
```

Expected: PASS.

### Task 4: Add a `TurnRouter` for profile-aware request construction

**Files:**
- Create: `src/voidx/agent/application/turn_router.py`
- Modify: `src/voidx/agent/application/agent_service.py`
- Modify: `src/voidx/agent/application/chat_service.py`
- Modify: `src/voidx/agent/application/coding_service.py`
- Test: `src/tests/test_agent/test_turn_router.py`
- Test: `src/tests/test_agent/graph/test_run_loop_startup.py`

- [ ] **Step 1: Write unit tests for coding request construction**

Input: coding session id and text.

Expected:

```python
request.profile.profile_id == "coding"
request.context.thread_id == session_id
request.context.session_id == session_id
request.context.tool_policy is None or request.context.tool_policy.is_coding_default
```

- [ ] **Step 2: Write unit tests for chat request construction**

Input: chat session id, workspace, and text.

Expected:

```python
request.profile.profile_id == "chat"
request.thread.thread_id == f"chat:{session_id}"
request.context.workspace == workspace
request.context.tool_policy.check_tool_call("bash", {}).allowed is False
```

- [ ] **Step 3: Implement `TurnRouter`**

Responsibilities:

- resolve `thread_id` from explicit parameter or `context.thread_id`
- load `SessionInfo` when target thread exists
- select `CHAT_PROFILE` for `runtime_profile="chat"`
- select `CODING_PROFILE` otherwise
- build `TurnRequest`

- [ ] **Step 4: Simplify `AgentService._handle_user_input()`**

Replace profile branching with:

```python
request = await self._turn_router.build_request(user_text, thread_id=thread_id, context=context)
await self._runtime.run_turn(request)
```

If `CodingService` and `ChatService` remain, they should delegate to the same request builder and not duplicate context construction.

- [ ] **Step 5: Fix context-only routing**

At the edge:

```python
effective_thread_id = thread_id or (context.thread_id if context is not None else "")
```

The red test from Task 1 should now pass.

- [ ] **Step 6: Run focused routing tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_turn_router.py src/tests/test_agent/graph/test_run_loop_startup.py -v
```

Expected: PASS.

### Task 5: Bind profile context into per-thread graph state

**Files:**
- Modify: `src/voidx/agent/infrastructure/langgraph/runtime/thread_context.py`
- Modify: `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py`
- Modify: `src/voidx/agent/infrastructure/langgraph/execution.py`
- Test: `src/tests/test_agent/graph/test_session_persistence.py`
- Test: `src/tests/test_agent/graph/test_chat_e2e.py`

- [ ] **Step 1: Add failing concurrent profile isolation test**

Run two concurrent turns:

- session A: coding profile
- session B: chat profile

Inside fake graph capture active profile/tool policy from current thread state.

Expected:

```python
assert captured[coding_id]["profile"] == "coding"
assert captured[chat_id]["profile"] == "chat"
assert captured[coding_id]["tool_policy"] != captured[chat_id]["tool_policy"]
```

- [ ] **Step 2: Extend `ThreadExecutionState`**

Add fields:

```python
turn_context: TurnExecutionContext | None = None
runtime_profile: RuntimeProfile | None = None
tool_policy: ToolPolicy | None = None
workspace: str = ""
```

Prefer storing the whole context plus convenience accessors only if needed.

- [ ] **Step 3: Bind context in `bind_thread_execution_context()`**

Accept `turn_context` and store it on the state before setting the contextvar.

- [ ] **Step 4: Stop writing shared host profile fields**

Remove:

```python
host._active_chat_tool_view = ...
host._active_profile = ...
```

from `TurnRunner`.

- [ ] **Step 5: Replace host field reads**

Update `LangGraphExecution` prompt/tool filtering code to read:

```python
state = current_thread_execution_state()
policy = state.tool_policy if state is not None else None
profile = state.runtime_profile if state is not None else None
```

- [ ] **Step 6: Run focused graph isolation tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/graph/test_session_persistence.py src/tests/test_agent/graph/test_chat_e2e.py -v
```

Expected: PASS.

### Task 6: Enforce tool policy at both LLM-visible and execution boundaries

**Files:**
- Modify: `src/voidx/agent/infrastructure/langgraph/execution.py`
- Modify: `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py`
- Modify: `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/helpers.py` if helper extraction is cleaner
- Test: `src/tests/test_agent/graph/test_chat_e2e.py`
- Test: `src/tests/test_agent/graph/test_execute_tools_guard.py`

- [ ] **Step 1: Add failing test for execution-time `file_path` denial**

Construct an AI tool call:

```python
{"name": "read", "args": {"file_path": "/outside/secret.txt"}, "id": "call_1"}
```

Expected tool result metadata:

```python
{"error": True, "tool_denied": True, "reason": "resource_out_of_scope"}
```

- [ ] **Step 2: Add failing test for hidden tool denial**

Even if the model somehow emits `bash`, chat execution should return `tool_denied` and never reach permission approval.

- [ ] **Step 3: Filter LLM-visible tools through policy**

When preparing tool definitions:

```python
tool_defs = [tool for tool in tool_defs if policy.allows_tool(tool["name"])]
```

Use the generic policy contract, not chat-specific names.

- [ ] **Step 4: Recheck full tool call before execution**

In `execute_one`, call:

```python
decision = policy.check_tool_call(tid, targs)
```

Return structured denied result if not allowed.

- [ ] **Step 5: Keep coding permission path unchanged**

If no tool policy is active or policy is coding default, continue using the existing permission service.

- [ ] **Step 6: Run focused tool tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/graph/test_chat_e2e.py src/tests/test_agent/graph/test_execute_tools_guard.py -v
```

Expected: PASS.

### Task 7: Make frontend/session profile-aware

**Files:**
- Modify: `src/voidx/ui/gateway/session/method/sessions.py`
- Modify: `src/voidx/ui/gateway/session/core.py`
- Modify: `src/voidx/ui/protocol/v2/threads.py`
- Modify: `frontend/src/ui/sidebar.ts`
- Modify: `frontend/src/main.ts`
- Test: `src/tests/test_ui/gateway/test_gateway_v2_dispatch.py`
- Test: `src/tests/test_ui/gateway/test_gateway_v2_routing.py`
- Test: `frontend/test/ui/workbench.test.ts`
- Test: `frontend/test/ui/sidebar.test.ts`

- [ ] **Step 1: Add frontend type coverage for `runtime_profile`**

Extend `ThreadInfo`:

```ts
runtime_profile?: "coding" | "chat" | string;
```

- [ ] **Step 2: Make empty session reuse profile-aware**

Change:

```ts
findReusableEmptyThread(directory)
```

to:

```ts
findReusableEmptyThread(directory, profile = "coding")
```

Only reuse a thread when workspace and runtime profile both match.

- [ ] **Step 3: Decide UI command shape**

Minimum viable option:

- Keep current “New session” button as coding.
- Add a separate chat creation path only if the UI already has a product concept for chat.

If adding chat:

```ts
rpcCall("session.create", { directory, profile: "chat" })
```

- [ ] **Step 4: Return profile in session create response**

`session.create` should return:

```python
{"runtime_profile": info.runtime_profile}
```

- [ ] **Step 5: Regenerate protocol schema**

Run:

```bash
./python.py scripts/export_ui_protocol_schema.py
```

Then run:

```bash
./test.py --frontend -- test/ui/workbench.test.ts test/ui/sidebar.test.ts -v
```

Expected: PASS.

### Task 8: Add boundary and cleanup tests

**Files:**
- Modify: `src/tests/test_agent/domain/test_import_boundaries.py`
- Modify: `src/tests/test_agent/graph/test_chat_e2e.py`
- Modify: `src/voidx/agent/infrastructure/langgraph/execution.py`

- [ ] **Step 1: Assert no `_active_chat_tool_view` production usage**

AST/string boundary test should fail if production code references:

```python
_active_chat_tool_view
_active_profile
```

- [ ] **Step 2: Assert runtime profile context only flows through `TurnExecutionContext`**

Allow UI protocol fields and domain models, but prevent new ad hoc host flags.

- [ ] **Step 3: Remove duplicated import blocks in `execution.py` only if touched nearby**

Do not broad-refactor `execution.py`; remove duplicate imports that are adjacent to changed prompt/tool policy code.

- [ ] **Step 4: Run boundary tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/domain/test_import_boundaries.py -v
```

Expected: PASS.

### Task 9: Regression suite

**Files:**
- No production changes unless failures reveal profile-context regressions.

- [ ] **Step 1: Run focused agent runtime suite**

Run:

```bash
./test.py --backend -- src/tests/test_agent -v
```

Expected: PASS.

- [ ] **Step 2: Run gateway/UI backend suite**

Run:

```bash
./test.py --backend -- src/tests/test_ui/gateway -v
```

Expected: PASS.

- [ ] **Step 3: Run focused frontend suite**

Run:

```bash
./test.py --frontend -- test/ui/workbench.test.ts test/ui/sidebar.test.ts test/main/main.test.ts -v
```

Expected: PASS.

- [ ] **Step 4: Run full backend when focused suites are green**

Run:

```bash
./test.py --backend
```

Expected: PASS.

- [ ] **Step 5: Check formatting and whitespace**

Run:

```bash
git diff --check
```

Expected: no output.

## 6. Acceptance Criteria

- [ ] `AgentRuntime.run_turn()` remains the only production turn entry.
- [ ] Gateway submit to a chat session reaches chat profile without explicit `thread_id` duplication.
- [ ] Concurrent coding/chat turns keep independent profile, workspace, prompt, and tool policy state.
- [ ] No production code reads `_active_chat_tool_view` or `_active_profile`.
- [ ] Chat tool policy denies write/shell/agent tools and out-of-scope local reads before execution.
- [ ] Chat MCP access allows discovery/load only; real MCP calls are denied until capability metadata exists.
- [ ] Frontend session creation/listing/reuse preserves `runtime_profile`.
- [ ] Existing coding behavior, permission prompts, session restore, compaction, and transcript persistence remain unchanged.

## 7. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Context refactor accidentally changes coding permission behavior | Keep `CodingToolPolicy` as pass-through and run `src/tests/test_agent` before broad changes. |
| Chat workspace differs from host workspace | Store workspace in `TurnExecutionContext` and use it for `AgentState["workspace"]` and `ToolContext.workspace`. |
| Frontend naming conflates “chat” with “coding session” | Preserve existing button as coding unless a separate chat affordance is added; make reuse profile-aware regardless. |
| MCP denial is too strict for useful chat | Treat as intentional until MCP catalog exposes read/write capability; document the limitation in tests. |
| `execution.py` changes grow too broad | Only replace active profile/tool policy access; defer large file decomposition to a separate plan. |

## 8. Suggested Commit Slices

1. `test: cover profile context routing gaps`
2. `feat: add turn execution context contract`
3. `feat: make chat tool policy call-aware`
4. `feat: route turns through profile-aware request builder`
5. `feat: bind profile policy in thread execution state`
6. `feat: enforce chat tool policy at execution boundary`
7. `feat: preserve runtime profile in web sessions`
8. `test: assert profile context boundaries`
