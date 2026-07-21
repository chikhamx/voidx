---
name: agent-runtime-phase-2-closure-plan
display_name: Agent Runtime 契约闭环 + Chat 端到端接入
description: 修复 Phase 1 契约歧义、清理死代码，并让 chat 通过 profile 路由端到端跑通，为 loop/goal 铺路
doc_type: tasks
audience: llm
status: proposed
source_design: docs/design/agent-runtime-chat.md
---

# Agent Runtime 契约闭环 + Chat 端到端接入

## 1. Goal

让 agent 基建契约闭环、chat 通过 profile 路由端到端可用，同时保持 coding 行为不变。修复 review 发现的三个契约歧义，清理死代码，把 chat 的 `ChatContext` 正确桥接到 thread 执行上下文，并通过 composition + gateway profile 路由让 chat session 真实跑通 `AgentRuntime → LangGraph`。

目标执行路径（coding 与 chat 同一条 runtime 链）：

```text
composition.build_agent_app()
  → AgentService / ChatService (profile 路由)
  → AgentRuntime.run_turn(TurnRequest)
  → LangGraphTurnEngine → LangGraphExecution.run_turn()
  → TurnRunner（thread_context 绑定 authoritative session + tool_view）
```

## 2. Review 结论回顾（待修复问题）

第一阶段功能 PASS（全量 3642 通过），但契约表面有 3 个会在 chat/loop 复用时放大的歧义，加死代码：

| # | 问题 | 位置 |
|---|------|------|
| 1 | `run_turn` 的 kwargs 覆写（session_id/user_text/...）零调用方，可绕过 frozen `TurnRequest` 静默改 identity | `runtime/runtime.py:43-57` |
| 2 | lazy identity 用 `getattr(turn_engine, "session_id", None)` 嗅探，而 `TurnEngine` Protocol 未声明；缺失时静默不保存 | `runtime/runtime.py:83` |
| 3 | facade 在 `session_id` 存在时无条件从 store 重载 runtime，丢弃 `TurnRequest.runtime` 输入 | `runtime/runtime.py:58-59` |
| 4 | 死代码：`AgentRuntime.run()`（与 `TurnService.run` 重复、零调用方）；`composition.py` 未用 import；`TurnService` 无 production 调用方 | 多处 |
| 5 | `advance_turn()`/`TurnExecution` 仅被自身单测使用，未接入 facade | `domain/turn.py` |
| 6 | 缺"coding 只有一个 turn 入口"的 AST 边界断言（计划 Task 5） | `domain/test_import_boundaries.py` |

## 3. Chat 端到端缺口（当前未闭环）

| # | 缺口 | 证据 |
|---|------|------|
| A | `ChatService` 未被任何 composition/UI 装配或调用（无 `build_chat_app`，gateway 无 chat 触发） | `chat_service.py` 仅测试引用 |
| B | `ChatService` 把 `ChatContext` 塞进 `TurnRequest.context`，但 `TurnRunner` 只认 `ThreadExecutionContext(session_id)`；不会把 chat session 绑定为 thread、也不会带 `tool_view`，故生产链 `_active_chat_tool_view` 永远 `None` | `turn_runner.py:114-116` |
| C | facade 丢弃 `ChatService` 传入的 `runtime_state`（同问题 3） | `runtime/runtime.py:58-59` |

## 4. Design Decisions

### 4.1 契约闭环

1. **删除 `run_turn` 的 kwargs 覆写**：`run_turn(request: TurnRequest)` 只接受 frozen request，identity/输入唯一来自 request。删除 `AgentRuntime.run()` 兼容方法（零调用方）。
2. **`TurnEngine` Protocol 显式声明 `session_id`**：facade 不再用 `getattr` 嗅探。`LangGraphTurnEngine.session_id` 已有，提升为 Protocol 契约；fake engine 测试同步实现。
3. **runtime 输入语义明确**：`TurnRequest.runtime` 是调用方提供的输入 snapshot。facade 的加载规则改为——`session_id` 存在且调用方**未显式提供** runtime 时才从 store 加载；调用方显式传入（chat 总是传入）则以调用方为准。用哨兵区分"未传"与"显式传默认值"。
4. **`advance_turn`/`TurnExecution` 接入 facade**：facade 用 `advance_turn` 驱动 RUNNING/COMMITTED 迁移，删除手工 `model_copy` 链。
5. **删除 `TurnService`**：无 production 调用方，迁移其测试到直接用 `AgentRuntime`。
6. **加 AST 边界断言**：production 中 `LangGraphExecution.run_turn` 只能被 `LangGraphTurnEngine` 调用；`AgentRuntime.run_turn` 是唯一 application 入口。

### 4.2 Chat 桥接（关键）

`ChatContext` 当前进不了 `ThreadExecutionContext`（frozen dataclass，只有 thread_id/session_id）。方案：

- 扩展 `ThreadExecutionContext` 增加可选 `tool_view` 字段（默认 `None`，coding 不受影响）。
- `ChatService.run_turn` 构造 `ThreadExecutionContext(thread_id, session_id, tool_view)` 作为 `TurnRequest.context`，而非裸 `ChatContext`；`scope`/`tool_view` 由 ChatService 解析后放入。
- `TurnRunner` 已有 `host._active_chat_tool_view = getattr(context, "tool_view", None)`，桥接后即生效。
- facade 用 `thread.session_id`（chat 总是设置）绑定 authoritative session；`thread_context` 已处理 borrowed/lazy session 绑定与 state restore。

### 4.3 Composition + profile 路由

- `build_agent_app()` 同时构造 `ChatService(runtime)`，与 `AgentService` 共享同一 `AgentRuntime`。
- `AgentFacade` 暴露 chat 入口；gateway submit 按目标 session 的 `runtime_profile` 路由：`chat` → `ChatService.run_turn`，否则 → coding `_handle_user_input`。
- 路由只读 session 的 `runtime_profile` 判别，不引入 `InteractionMode` 推断（符合设计 §10）。

## 5. Scope

### 5.1 In scope
- 修复问题 1–6（契约闭环 + 清理）。
- `ThreadExecutionContext.tool_view` 扩展 + `ChatService` 桥接。
- composition 装配 `ChatService`；gateway profile 路由。
- chat 端到端集成测试（真实 `LangGraphExecution` + fake graph），验证 tool policy 强制、session 隔离、lazy identity 返回。
- coding 全量回归。

### 5.2 Out of scope
- 前端 UI 的 chat 触发界面（gateway 协议层 chat 入口适配另列）。
- loop scheduler、goal/workflow lifecycle。
- `RuntimeProfile` 的 tool/prompt/interaction policy 子对象（本期 `ChatToolView` 已在 domain 表达工具边界；policy 子对象留待 loop/goal 需要时再加）。
- MCP read/write 分类。

## 6. Ownership and Commit Rules（沿用 Phase 1，不变）

- `AgentRuntime` 是 turn event 与 runtime state commit 唯一 owner；`ChatService`/`AgentService` 不二次保存。
- LangGraph infrastructure 继续负责 message/transcript persistence。
- borrowed/lazy identity 由 `thread_context` + facade resolve，`TurnResult.session_id` 返回最终 identity。

## 7. Target File Changes

| Path | Action | Responsibility |
|------|--------|----------------|
| `src/voidx/agent/runtime/runtime.py` | modify | 删 kwargs 覆写与 `run()`；engine.session_id 契约；runtime 输入哨兵；`advance_turn` 接入 |
| `src/voidx/agent/ports/turn_engine.py` | modify | Protocol 声明 `session_id` |
| `src/voidx/agent/application/turn_service.py` | delete | 无 production 调用方，测试迁到 AgentRuntime |
| `src/voidx/agent/application/chat_service.py` | modify | 桥接 `ThreadExecutionContext(tool_view)`；显式 runtime 传入 |
| `src/voidx/ui/output/types.py` | modify | `ThreadExecutionContext` 加 `tool_view` 可选字段 |
| `src/voidx/agent/composition.py` | modify | 装配 `ChatService`；删未用 import |
| `src/voidx/agent/facade.py` | modify | 暴露 chat 路由入口 |
| `src/voidx/agent/application/agent_service.py` | modify | gateway submit 按 `runtime_profile` 路由 chat/coding |
| `src/tests/test_agent/runtime/test_runtime.py` | modify | kwargs 删除、哨兵语义、advance_turn 接入 |
| `src/tests/test_agent/application/test_services.py` | modify | TurnService 测试迁移 |
| `src/tests/test_agent/test_chat_service.py` | modify | 桥接断言（context 携带 session_id+tool_view） |
| `src/tests/test_agent/domain/test_import_boundaries.py` | modify | 单入口 AST 断言 |
| `src/tests/test_agent/graph/test_chat_e2e.py` | create | chat 端到端：policy 强制 + 隔离 + lazy identity |

## 8. Implementation Tasks

### Task 1: 契约闭环
- [ ] `TurnEngine` Protocol 声明 `session_id`；facade 删 `getattr` 嗅探。
- [ ] 删 `run_turn` kwargs 覆写与 `AgentRuntime.run()`。
- [ ] runtime 输入哨兵：区分"未传 runtime"与"显式传入"，加载规则修正。
- [ ] facade 用 `advance_turn` 驱动迁移。
- [ ] 删 `TurnService` 并迁移测试；删 composition 未用 import。
- [ ] focused: `./test.py --backend -- src/tests/test_agent/runtime src/tests/test_agent/application -v`

### Task 2: 边界断言
- [ ] AST 断言：`AgentRuntime.run_turn` 为唯一 application 入口；`LangGraphExecution.run_turn` 仅被 engine adapter 调用。
- [ ] focused: `./test.py --backend -- src/tests/test_agent/domain -v`

### Task 3: Chat 桥接
- [ ] `ThreadExecutionContext.tool_view` 可选字段。
- [ ] `ChatService` 构造带 `session_id`+`tool_view` 的 `ThreadExecutionContext`；显式传 runtime。
- [ ] focused: `./test.py --backend -- src/tests/test_agent/test_chat_service.py src/tests/test_agent/test_chat_policy.py -v`

### Task 4: Composition + profile 路由 + chat 入口
- [ ] composition 装配 `ChatService` 并与 `AgentService` 共享同一 `AgentRuntime`。
- [ ] `session.create` JSON-RPC 方法支持可选 `profile` 参数（`"chat"` 建 chat session）；不新增 `UiCommand`。
- [ ] `session.submit` / `_handle_user_input` 按目标 session 的 `runtime_profile` 路由：`chat` → `ChatService.run_turn`，否则走 coding 路径。
- [ ] focused: `./test.py --backend -- src/tests/test_agent/test_composition.py -v`

### Task 5: Chat 端到端
- [ ] 真实 `LangGraphExecution` + fake graph：chat turn 完成、tool policy 拒绝写工具、session 隔离不污染 coding、lazy identity 经 `TurnResult` 返回。
- [ ] focused: `./test.py --backend -- src/tests/test_agent/graph/test_chat_e2e.py -v`

### Task 6: 回归
- [ ] `./test.py --backend -- src/tests/test_agent -v`
- [ ] `./test.py --backend`
- [ ] `git diff --check`

## 9. Global Invariants

1. coding 的 prompt、工具、权限、compaction、slash、topology、session 语义不变。
2. `AgentRuntime.run_turn()` 是所有 profile 的唯一 production turn 入口。
3. runtime state / message / turn event 各自单一 commit/publish owner。
4. borrowed/lazy identity 不从宿主 session 推断；最终 identity 经 `TurnResult` 返回。
5. chat 工具边界由 `ChatToolView` 在执行边界强制，不靠 prompt；deny 不转 coding 审批。
6. shared resources 与 thread-scoped mutable state 分离。

## 10. Acceptance Criteria

- [ ] 问题 1–6 全部修复并有测试证据。
- [ ] chat 端到端跑通：普通问答 + 只读工具允许 + 写工具拒绝 + session 隔离 + lazy identity。
- [ ] coding 全量回归通过（普通/borrowed/lazy/取消/异常/exactly-once）。
- [ ] 完整 backend suite 与 `git diff --check` 通过。
- [ ] `TurnEngine.session_id`、`ThreadExecutionContext.tool_view` 成为后续 loop/goal 可复用的显式契约。

## 11. Risks

| Risk | Mitigation |
|------|------------|
| 哨兵语义改动影响 coding 加载路径 | 保持"未传 runtime 且 session 存在则从 store 加载"的 coding 现状；chat 显式传入 |
| `ThreadExecutionContext` 加字段影响既有构造 | 新字段默认 `None`，所有现有调用点不传即可 |
| 删 `TurnService` 破坏测试辅助 | `run_loop_helpers.py` 改用 `AgentRuntime` 直接构造 |
| profile 路由误判 session | 只读 `runtime_profile` 字段，缺省 `coding`；chat 必须显式 `profile="chat"` |
