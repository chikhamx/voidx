> **Status: Done** — Archived on 2026-07-25.

---
name: agent-runtime-phase-1-plan
display_name: Agent Runtime Unification 第一期实施计划
description: 抽出可复用 Agent Runtime 基建并接入 coding，暂不实现 chat/loop 模式
doc_type: tasks
audience: llm
status: proposed
source_design: docs/design/agent-runtime-unification.md
---

# Agent Runtime Unification 第一期实施计划

## 1. Goal

第一期直接抽出可被后续 chat、loop、goal 复用的 Agent Runtime 基建，并让现有 coding 主路径接入它。第一期不实现 chat/loop/goal 的完整语义，但后续模式不得再复制一套执行、thread、session 和 resource 基建。

目标执行路径：

```text
AgentService
  → AgentRuntime.run_turn()
  → LangGraphTurnEngine / LangGraphExecution
  → TurnRunner（暂作为 graph adapter 内部实现）
```

`AgentRuntime` 是第一期真正落地的单轮 runtime facade；coding 是第一个 runtime profile/调用方。现有 coding 的输入、prompt、工具、权限、compaction、LangGraph topology、slash 行为和 session 持久化语义保持不变。

## 2. Design Principles

1. **基建先于模式。** Runtime 负责一次 turn 的资源、thread identity、执行和提交边界；coding 只提供 profile 和输入，chat/loop 后续复用同一入口。
2. **渐进迁移，不平行实现。** 新 runtime 内部复用现有 LangGraph、tool、context、compaction 和 `TurnRunner` 能力；完成接入后 coding 不再直接绕过 runtime 调用另一套 turn 链路。
3. **唯一提交 owner。** runtime 必须明确 runtime state、message/transcript 和 turn event 的提交责任，第一期禁止 `TurnService`/`TurnRunner` 与 runtime 重复保存。
4. **先保护行为，再替换边界。** 迁移前先建立 ordinary、borrowed、lazy、取消和异常基线；迁移后使用同一组测试证明行为等价。
5. **后续模式只扩 profile/lifecycle。** chat、loop、goal、workflow 和 scheduler 后续只能建立在本期 runtime/thread/resource 契约上，不得新增平行执行器。

## 3. Current Architecture and Target Boundary

### 3.1 Current path

```text
AgentService
  → TurnService.run()
  → TurnEngine.run()
  → LangGraphExecution.run_turn()
  → TurnRunner.run_once()
```

当前 `TurnRunner` 负责 graph turn 的消息加载、lazy session 创建、上下文准备、graph 执行和部分结果保存；`TurnService` 负责 turn event 以及成功/异常/取消路径的 runtime state 保存。两处重叠是本期必须在接入前解决的迁移点。

### 3.2 Target Phase 1 path

```text
AgentService
  → AgentRuntime.run_turn(TurnRequest)
      ├── resolve AgentThread / session identity
      ├── load and prepare SessionRuntimeState
      ├── invoke TurnEngine / LangGraph adapter
      ├── commit turn result exactly once
      └── publish lifecycle events
          → existing LangGraph topology
```

`TurnRunner` 在第一期可以继续作为 LangGraph infrastructure adapter，但不能再独立承担 runtime facade 之外的最终提交。`TurnService` 迁移为 coding compatibility/application adapter，或被删除；最终方案必须只有一个 production caller 和一个 persistence owner，不能长期保留两条入口。

## 4. Scope

### 4.1 In scope

- 将旧 session state `AgentRuntime` 重命名为 `SessionRuntimeState`，不保留 alias，释放 `AgentRuntime` 为执行 facade 名称。
- 新增 runtime domain/application contracts：
  - `RuntimeProfile`：coding profile 的稳定 identity 和策略入口；
  - `AgentThread`：thread/session identity 与 borrowed/lazy 语义；
  - `TurnRequest` / `TurnExecution`：runtime 的单轮输入、identity 和执行上下文；
  - `TurnResult`：最终 session identity、state、事件/消息提交结果的明确边界；
  - `LifecycleState`：本期只覆盖 running/completed/failed/cancelled 所需状态。
- 新增 `AgentRuntime` facade，组合现有 `TurnEngine`、session store、event publisher 和 LangGraph execution 所需资源。
- 把 coding 的 `AgentService` 调用迁移到 `AgentRuntime.run_turn()`；coding 使用现有默认 profile，外部行为保持兼容。
- 统一 ordinary、borrowed target session、no-session first turn 的 authoritative identity；lazy 创建后的最终 session id 必须通过 `TurnResult` 返回。
- 选定并实现唯一 persistence owner，覆盖成功、异常、取消路径；增加写入次数测试防止双写。
- 保留现有 LangGraph topology、tool loop、prompt 编译、permission 流程、compaction 和 UI event 语义。
- 建立后续模式所需的 resource boundary，但只抽象当前实际资源，不实现 loop/chat 专属能力。

### 4.2 Out of scope

- chat、loop、goal、workflow、scheduler 的模式迁移和完整生命周期语义；
- 多 thread 持久化 store、数据库 schema、session migration 和跨 runtime 依赖；
- 并发 runtime、lease/fencing、recovery、outbox 和分布式调度；
- 新的 frontend protocol、UI protocol、tool schema 或权限产品行为；
- 修改 LangGraph topology、模型调用策略、tool loop、compaction 算法或 slash 命令语义；
- 为兼容旧调用方长期保留第二条 runtime 入口、双写、旧状态 alias 或 parallel adapter；
- 让第一期假装实现 loop/chat，只为它们提前添加没有当前调用方的业务字段。

## 5. Compatibility Baseline

迁移前后必须通过同一组测试，除内部类型名和调用层次外不得改变：

| Scenario | Required evidence |
|---|---|
| ordinary coding turn | prompt、工具、权限、message/transcript、runtime state 和 session persistence 结果不变 |
| borrowed target session | identity 来自 `ThreadExecutionContext`，目标 session 更新，宿主 session/state/message 不被污染 |
| no-session first turn | runtime 创建 session 后，user/assistant message、runtime snapshot 和 `TurnResult.session_id` 一致 |
| startup restore / slash / clear / resume | 现有 coding 测试继续通过；runtime 不吞掉或重解释这些 application 语义 |
| cancellation | cancel event、rollback 和已写 state 与迁移前一致，且只提交一次 |
| exception | failed event、异常传播和 state 提交与迁移前一致，且只提交一次 |
| compaction/topology | prepare/call_llm/execute_tools/finalize 顺序和 compaction 语义不变 |
| composition | production 只保留 `AgentRuntime.run_turn()` 作为 coding 单轮入口 |

## 6. Runtime Contracts

### 6.1 `SessionRuntimeState`

**Path:** `src/voidx/agent/domain/state.py`

现有字段、默认值、Pydantic dump/validate、deep-copy 和 persistence snapshot schema 保持不变，只将类名改为 `SessionRuntimeState`。不得定义 `AgentRuntime = SessionRuntimeState`。

### 6.2 `RuntimeProfile`

**Path:** `src/voidx/agent/domain/profile.py`

定义 frozen profile descriptor，至少包含 `profile_id`、`revision` 和 `name`；第一期允许附带 coding 当前需要的 prompt/tool/permission policy references，但不复制策略实现。profile 必须可被 runtime 读取，不能持有 mutable session state。

### 6.3 `AgentThread`

**Path:** `src/voidx/agent/domain/thread.py`

定义 thread identity descriptor：`thread_id`、`session_id`、可选 `parent_thread_id` 和生命周期状态。`session_id` 在 lazy first turn 前可为空；borrowed turn 的 authoritative identity 必须来自 `ThreadExecutionContext`，不能使用宿主 `execution.session_id` 覆盖它。thread 不直接持有 transcript 或 store。

### 6.4 `TurnRequest` / `TurnExecution` / `TurnResult`

**Path:** `src/voidx/agent/domain/turn.py` 或 `src/voidx/agent/runtime/contracts.py`

- `TurnRequest`：user input、display input、profile、thread/context 和当前 runtime state 输入。
- `TurnExecution`：runtime 内部不可变的 resolved identity、lifecycle 和执行 metadata。
- `TurnResult`：最终 `thread_id`/`session_id`、resulting `SessionRuntimeState`、提交后的消息/事件摘要和错误/取消结果。

这些类型必须明确“输入 snapshot”和“提交结果”的边界；不得把 mutable host、具体 LangGraph state 或 concrete adapter 暴露给后续模式。

### 6.5 `RuntimeResources`

**Path:** `src/voidx/agent/ports/runtime_resources.py`

定义只读 Protocol，覆盖第一期 coding 实际需要的 model/tool/context/permission/compaction/session/event 能力。Protocol 不负责构造资源、不拥有 mutable thread state，也不强迫 `ExecutionHost` 增加 adapter-only 属性。后续 chat/loop 可通过同一资源边界提供不同 profile-scoped view。

## 7. Ownership and Commit Rules

第一期必须形成可执行的 owner 结论，而不是只写观察记录：

| State/resource | Phase 1 owner rule |
|---|---|
| thread/session identity | `AgentRuntime` 根据 request/context resolve；lazy 创建后的 identity 由 runtime 返回 `TurnResult` |
| mutable session runtime state | runtime 负责 turn 内 snapshot 和最终提交；application 不得再次保存同一结果 |
| messages/transcript | 沿用现有 session adapter，但由 runtime 的单一 commit boundary 触发 |
| turn events | runtime 发布 started/completed/failed/cancelled；compatibility adapter 不得重复发布 |
| graph execution | `TurnRunner`/LangGraph 保持 infrastructure 实现，runtime 负责调用和结果归一化 |
| shared resources | composition 构造并注入 runtime；thread-scoped state 不放入 shared resource |

`TurnService` 的迁移方式必须在实现前确定：若保留，只能成为无独立执行或保存逻辑的薄 compatibility wrapper；若删除，必须一次性迁移全部调用方。不得让 `TurnService` 和 `AgentRuntime` 同时保存 state 或发布同一 turn event。

## 8. Target File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/voidx/agent/runtime/runtime.py` | create | `AgentRuntime.run_turn()` facade、resolve/execute/commit 生命周期 |
| `src/voidx/agent/runtime/contracts.py` | create if needed | `TurnRequest`、`TurnResult` 等 runtime-facing contracts |
| `src/voidx/agent/runtime/__init__.py` | create/modify | runtime public exports |
| `src/voidx/agent/domain/state.py` | modify | `SessionRuntimeState` 重命名 |
| `src/voidx/agent/domain/profile.py` | create/modify | `RuntimeProfile` |
| `src/voidx/agent/domain/thread.py` | create/modify | `AgentThread`、`LifecycleState` |
| `src/voidx/agent/domain/turn.py` | modify | `TurnExecution` 与 turn contract |
| `src/voidx/agent/ports/runtime_resources.py` | create | runtime 所需资源 Protocol |
| `src/voidx/agent/composition.py` | modify | 构造并注入 `AgentRuntime` |
| `src/voidx/agent/application/agent_service.py` | modify | coding turn 改走 runtime facade |
| `src/voidx/agent/application/turn_service.py` | modify/delete | 迁移为薄 wrapper 或原子删除，禁止保留双执行路径 |
| `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py` | modify | 保持 graph 执行，移除重复最终提交 |
| 现有 state mapper/session/adapter/ports 文件 | modify | state 类型迁移和 facade 适配，行为不变 |
| `src/tests/test_agent/runtime/` | create | runtime contract、commit、identity 和 lifecycle tests |
| `src/tests/test_agent/graph/test_session_persistence.py` | modify | ordinary/borrowed/lazy 回归 |
| `src/tests/test_agent/test_composition.py` | modify | runtime wiring 和单入口断言 |
| `src/tests/test_agent/domain/test_import_boundaries.py` | modify | domain/runtime 依赖方向和阶段边界 |

## 9. Implementation Tasks

### Task 0: 建立迁移前 RED/GREEN 基线

- [x] 先运行现有 coding/session 测试并记录 baseline。
- [x] 补齐 ordinary、borrowed、lazy、取消和异常的关键断言；新增测试必须先用有效隔离方式确认失败原因。
- [x] 对 persistence adapter 和 event publisher 加 spy，记录当前写入/发布次数，作为接入后的等价基线。

```bash
./test.py --backend -- src/tests/test_agent/graph/test_session_persistence.py src/tests/test_agent/test_composition.py -v
```

### Task 1: 重命名 session state

- [x] 将 `AgentRuntime` state 改为 `SessionRuntimeState`，迁移全部 import、annotation、constructor 和 mapper。
- [x] 不保留旧类名、旧 mapper alias 或 fallback import。
- [x] 运行 state、mapper、service、adapter focused tests。

### Task 2: 实现 runtime contracts 和 resources port

- [x] 为 `RuntimeProfile`、`AgentThread`、`LifecycleState`、`TurnRequest`、`TurnExecution`、`TurnResult`、`RuntimeResources` 编写 contract tests。
- [x] 实现最小字段、validation、immutability 和 identity 规则。
- [x] 确认 domain/runtime contracts 不导入 concrete infrastructure，resources Protocol 不复制 `ExecutionHost`。

```bash
./test.py --backend -- src/tests/test_agent/domain src/tests/test_agent/runtime -v
```

### Task 3: 抽出 AgentRuntime facade

- [x] 创建 `AgentRuntime.run_turn()`，按 resolve → prepare → execute → commit → publish 顺序组织一次 coding turn。
- [x] 将现有 `TurnRunner`/LangGraph execution 作为 injected engine/adapter 使用，不复制 graph topology、tool loop 或 context compiler。
- [x] 在 facade 内解析 ordinary、borrowed 和 lazy session identity，并将 lazy 最终 id 放入 `TurnResult`。
- [x] 明确错误/取消时的 state 和 message 提交边界，保证与 baseline 一致。

### Task 4: coding 接入和 persistence 单 owner

- [x] 在 composition root 构造 `AgentRuntime` 并注入现有 resources/engine/session/event 依赖。
- [x] 将 `AgentService` coding turn 改为调用 `AgentRuntime.run_turn()`。
- [x] 将 `TurnService` 迁移成无独立执行/保存/发布逻辑的薄 compatibility wrapper，或在一次原子变更中删除并迁移全部调用方。
- [x] 移除 `TurnRunner`/`TurnService` 与 runtime 重复的 commit/publish，加入 exactly-once spy tests。
- [x] 保证 slash、startup restore、clear、resume 仍由现有 application 语义处理，不在 runtime 中复制 command logic。

```bash
./test.py --backend -- src/tests/test_agent/runtime src/tests/test_agent/application src/tests/test_agent/graph -v
```

### Task 5: 阶段边界和回归

- [x] 增加 import/AST 检查：coding 只有一个 runtime turn 入口；没有旧 state alias 或第二个 execution facade。
- [x] 验证 prompt、tools、permissions、compaction、topology、UI events 和 session identity 与 baseline 一致。
- [x] 明确 chat/loop/goal 后续只复用 runtime contracts/facade，不在本期加入模式分支。

### Task 6: 全量验证和交接

```bash
./test.py --backend -- src/tests/test_agent -v
./test.py --backend
git diff --check
```

- [x] 所有 focused/backend tests 通过。
- [x] 记录实际 runtime owner、commit boundary 和后续模式复用点。
- [ ] 另起 Phase 2 plan，只设计 chat/loop/goal profile、独立 thread persistence、scheduler 和资源视图，不再重做 runtime 基建。

## 10. Global Invariants

1. coding 的业务输入、prompt、工具、权限、compaction、slash 语义和 LangGraph topology 不变。
2. `AgentRuntime.run_turn()` 是 coding 的唯一 production turn 入口；不得保留平行 runtime 执行链。
3. runtime state、message/transcript 和 turn event 各自只有一个 commit/publish owner。
4. borrowed identity 不得从宿主 session 推断；lazy-created session 的最终 identity 必须可从 `TurnResult` 获得。
5. `TurnRunner` 只负责 graph infrastructure 执行，不成为第二个 runtime facade。
6. shared resources 与 thread-scoped mutable state 分离。
7. 本期不实现 chat/loop/goal 业务语义，不修改无关 workspace 文件。

## 11. Acceptance Criteria

- [x] `SessionRuntimeState` 完成原子迁移，无旧 state alias。
- [x] `AgentRuntime.run_turn()` 已存在并被 coding production path 调用。
- [x] runtime contracts、resources port、composition wiring 有 focused tests。
- [x] ordinary、borrowed、lazy、取消、异常和现有 coding 状态行为回归通过。
- [x] persistence/event spy 证明没有双写或重复 publish。
- [x] `TurnRunner`/LangGraph topology、prompt、tools、permissions、compaction 行为未被复制或改变。
- [x] 完整 backend suite 和 `git diff --check` 通过。
- [x] chat/loop/goal 尚未实现，但可以直接复用本期 runtime facade、thread/profile/resource contracts。

## 12. Risks and Rollback

| Risk | Mitigation |
|---|---|
| 新 facade 与 TurnService/TurnRunner 双写 | 先做 baseline spy，再以 exactly-once 测试约束单一 commit owner |
| borrowed/lazy session identity 错配 | 真实 host/target 集成测试，最终 identity 只由 runtime resolve/result 返回 |
| facade 复制 LangGraph 逻辑 | runtime 只编排 injected engine/adapter，topology 继续由现有 infrastructure 拥有 |
| coding 行为被 runtime 重解释 | slash/profile 语义留在现有 application，迁移前后使用同一回归矩阵 |
| 为未来模式过早加入分支 | 本期只接 coding profile，chat/loop/goal 单独进入 Phase 2 |

回滚时优先整体回退 composition/AgentService 入口切换，再删除未被其他模式使用的 runtime facade；状态重命名必须作为原子变更回滚，不使用 alias 维持新旧模型。

## 13. Phase 2 Handoff

Phase 2 不再抽基础执行链，而是基于本期 runtime 继续实现：

1. chat profile 和 chat-specific interaction policy；
2. goal/workflow lifecycle；
3. loop scheduler、独立 thread/transcript 和 wakeup/stop 语义；
4. runtime-scoped resource views、permission isolation 和并发/recovery；
5. 多模式的 persistence schema 与 migration。

Phase 2 不得重新创建 `AgentRuntime.run_turn()`、第二套 LangGraph execution 或另一套 session state owner。

## 14. Definition of Done

- [x] Task 0–6 完成，RED/GREEN 或 characterization 证据可复现。
- [x] coding 已通过 `AgentRuntime.run_turn()` 执行，且 production 只有一个 turn 入口。
- [x] runtime 已拥有明确的 thread/session/turn identity、resource 注入和 commit boundary。
- [x] ordinary、borrowed、lazy、取消、异常回归通过，persistence/event exactly once 通过。
- [x] focused agent suite、完整 backend suite、`git diff --check` 全部通过。
- [ ] Phase 2 只扩展 chat/loop/goal 等模式，不重复建设 runtime 基建。
