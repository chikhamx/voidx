---
name: agent-architecture-refactor-plan
display_name: Agent 架构分层改造实施计划
description: 将 agent 重构为端口驱动应用核心，并保持 CLI、UI 协议和持久化兼容
doc_type: tasks
audience: llm
status: approved
---

# Agent 架构分层改造实施计划

## Goal

将 `src/voidx/agent` 从共享私有状态、Mixin 和大 Host Protocol 组成的 Graph 中心架构，迁移为“领域模型 + 应用服务 + 端口 + 基础设施适配器 + 单一组合根”；消除 `ui/tools -> agent` 反向引用及类型依赖环，同时保持 CLI 行为、UI protocol schema 和现有 session/runtime-state 持久化格式兼容。

## Architecture

LangGraph 保留为单轮执行引擎，但只存在于 `voidx.agent.infrastructure.langgraph`；session、compaction、tool execution、subagent 和 command 由 `application` 用例编排。运行态通过显式 `AgentRuntime` 传递，长期依赖通过小型 ports 注入，禁止组件访问 `host._private_state`。

依赖方向必须保持：

```text
presentation ─┐
infrastructure ├──> application ───> domain
composition ──┘             └──────> ports
```

`domain` 不依赖 LangGraph、UI、memory、tools、permission、workflow 或 config；`ports` 不依赖任何 concrete adapter。`src/voidx/main.py` 只调用 composition/facade，不构造执行引擎细节。

## Compatibility Contract

必须保持：

- `voidx` CLI 的参数、slash command 名称、用户可见语义和 exit code。
- `frontend/src/rpc/protocol.schema.json` 的协议兼容；本改造不得删除或改变已有字段语义。
- `src/voidx/memory/runtime_state.py` 当前数据库列、JSON 字段和历史 session 恢复行为。
- 每一阶段结束时主分支可运行，不允许依赖未来阶段才能恢复行为。

允许改变：

- `src/voidx` 内部 Python API 和导入路径。
- `VoidXGraph`、Graph mixin、Host Protocol、Slash adapter 等内部类型。
- 内部事件、domain DTO 和 adapter 组织方式。

## Tech Stack

- Python 3.12+、Pydantic v2、asyncio、LangGraph/LangChain。
- AST 架构测试和 pytest，统一通过 `./test.py --backend` 执行。
- UI schema 通过 `./python.py scripts/export_ui_protocol_schema.py` 导出并由 git diff 验证。

## Target File Structure

| Path | Responsibility |
|---|---|
| `src/voidx/agent/domain/state.py` | `AgentRuntime` 及 turn/session/context/guard 领域状态 |
| `src/voidx/agent/domain/events.py` | 与展示技术无关的 Agent 语义事件 |
| `src/voidx/agent/domain/turn.py` | Turn request/result 和纯状态转换规则 |
| `src/voidx/agent/domain/compaction.py` | compaction result、preflight DTO 和纯规则 |
| `src/voidx/agent/ports/*.py` | session、model、tools、permission、UI、workflow、turn engine 小端口 |
| `src/voidx/agent/application/agent_service.py` | 应用生命周期与动态 reconfigure |
| `src/voidx/agent/application/turn_service.py` | 单轮用例、持久化边界和事件顺序 |
| `src/voidx/agent/application/session_service.py` | session 恢复、清理、标题和 transcript 用例 |
| `src/voidx/agent/application/compaction_service.py` | compaction 用例编排 |
| `src/voidx/agent/application/tool_service.py` | permission + tool execution 用例编排 |
| `src/voidx/agent/application/subagent_service.py` | child agent 生命周期与结构化结果 |
| `src/voidx/agent/application/command_service.py` | slash command 对应应用操作 |
| `src/voidx/agent/infrastructure/langgraph/` | LangGraph engine、nodes、topology 和 state mapper |
| `src/voidx/agent/infrastructure/runtime_ui.py` | `AgentEvent` 到现有 runtime/UI event 的映射 |
| `src/voidx/agent/infrastructure/memory_session.py` | session port 到 `voidx.memory.service` 的适配 |
| `src/voidx/agent/infrastructure/tool_executor.py` | tools/permission 现有服务适配 |
| `src/voidx/agent/presentation/slash/` | slash 解析、command DTO 和结果呈现 |
| `src/voidx/agent/composition.py` | 唯一依赖组装点 |
| `src/voidx/agent/facade.py` | CLI/main 使用的稳定应用入口 |
| `src/voidx/runtime/execution_context.py` | 跨 Agent/UI 的 execution identity ContextVar |
| `src/voidx/tools/output_policy.py` | 工具输出限制与截断策略 |

上述目标文件按后续任务创建；不得先创建空壳并让新旧架构长期双轨运行。

## Global Invariants

1. 每个阶段先写目标行为/边界测试并确认 RED，再迁移最小完整垂直切片并确认 GREEN。
2. 同一状态在任一阶段只能有一个 owner；adapter 可映射，不得双写新旧 runtime。
3. Application 不得引用 `voidx.ui.*`、`voidx.memory.*` concrete module 或 LangGraph。
4. Infrastructure 不得反向被 `runtime`、`tools`、`ui`、`memory`、`workflow` 导入。
5. 不新增 `getattr(host, "_...")`、Graph Mixin 或超过 15 个成员的 Host Protocol。
6. Domain 类型不直接写数据库；持久化必须通过 mapper 转换为现有 `RuntimeStateSnapshot` 等 DTO。
7. 完成最终 verify 前不得归档本 spec。

## TDD Tasks

### T0 — 建立架构和兼容基线

- [ ] 扩展 `src/tests/test_agent/test_module_boundaries.py`：递归扫描运行时导入和 `TYPE_CHECKING` 导入，断言 `ui/tools/runtime/memory/workflow` 不得导入 `voidx.agent.graph`，并新增允许列表以记录 T1 将删除的两个现有 offender：`ui/output/events/bus.py`、`tools/file/types.py`。
- [ ] 新建 `src/tests/test_agent/test_dependency_cycles.py`：对 `src/voidx` 构建运行时导入图和完整类型图，输出 SCC 的具体边；运行时 SCC 必须立即为零，类型 SCC 将当前 5 模块环作为临时 expected debt，T4 删除。
- [ ] 新建/补齐协议基线测试，调用 `export_protocol_schema()` 与 `frontend/src/rpc/protocol.schema.json` 比较。
- [ ] 新建 `src/tests/test_memory/test_runtime_state_compatibility.py`，加入历史 payload/数据库 fixture 的 load-save-load 测试，覆盖 goal、workflow runs、todo、compaction summary 和 session time。
- [ ] 先运行新增约束并确认目标反向依赖/类型环导致 RED：`./test.py --backend -- src/tests/test_agent/test_module_boundaries.py src/tests/test_agent/test_dependency_cycles.py -v`；再单独运行兼容基线并确认 GREEN：`./test.py --backend -- src/tests/test_memory/test_runtime_state_compatibility.py -v`。
- [ ] 记录临时 debt 只允许精确路径，禁止通配符或新增 offender。

Acceptance：架构测试能在错误信息中列出 `source -> target`；UI schema 与历史持久化已有可重复基线。

### T1 — 消除 Agent 外部反向依赖

- [ ] 为 `src/voidx/runtime/execution_context.py` 在 `src/tests/test_runtime/test_execution_context.py` 写 RED：嵌套/并发 bind 隔离、缺省 identity、reset 正确。
- [ ] 创建 `ExecutionIdentity`、`current_execution_identity()`、`bind_execution_identity()`；将 `src/voidx/agent/graph/thread_context.py` 仅保留 Agent runtime state，不再作为 UI identity 来源。
- [ ] 修改 `src/voidx/ui/output/events/bus.py` 只依赖 `voidx.runtime.execution_context`；保持自动填充 `thread_id` 行为。
- [ ] 为 `src/voidx/tools/output_policy.py` 写工具输出策略 RED；创建独立的 `DEFAULT_TOOL_OUTPUT_MAX_CHARS`。
- [ ] 修改 `src/voidx/tools/file/types.py` 不再导入 Agent；`src/voidx/agent/tool_messages.py` 可依赖 tools policy 或定义独立 LLM replay 上限，禁止反向引用。
- [ ] 删除 T0 对两个 offender 的临时允许项。
- [ ] 验证：`./test.py --backend -- src/tests/test_runtime/test_execution_context.py src/tests/test_ui/output src/tests/test_tools/file/test_read.py src/tests/test_agent/test_module_boundaries.py -v`。

Acceptance：`grep`/AST 图中 `src/voidx` 非 Agent 包不再导入 `voidx.agent.graph`；`voidx.tools` 不导入任何 `voidx.agent`。

### T2 — 统一状态入口并建立纯 Domain

- [ ] 在 `src/tests/test_agent/domain/` 为 `AgentRuntime`、turn state conversion、compaction DTO 写 RED；测试不得构造 `VoidXGraph`。
- [ ] 创建 `src/voidx/agent/domain/{__init__,state,turn,compaction,events}.py`，只使用标准库、Pydantic、LangChain message 公共类型和 `voidx.runtime` 公共 DTO。
- [ ] 将 `CompactionResult`、`PreflightCompactionResult` 从 `graph/compaction_coordinator.py` 移到 `domain/compaction.py`，更新调用方。
- [ ] 将 Agent 内所有 `voidx.agent.task_state` 导入迁至 `voidx.runtime.task_state`。
- [ ] `src/voidx/agent/task_state.py` 暂保留兼容 re-export，但增加测试禁止 `src/voidx/agent/**` 内部引用该路径。
- [ ] 创建 persistence mapper 测试，证明 domain runtime 与现有 `RuntimeStateSnapshot` 往返不改变 JSON/数据库字段。
- [ ] 验证：`./test.py --backend -- src/tests/test_agent/domain src/tests/test_agent/test_module_boundaries.py src/tests/test_agent/graph/test_session_runtime_state.py -v`。

Acceptance：Domain 可独立导入；Agent 内部状态类型只有 `voidx.runtime.task_state` 一个入口；持久化 fixture 无变化。

### T3 — 建立小端口和垂直应用服务

- [ ] 创建 `src/voidx/agent/ports/`，按 `session.py`、`events.py`、`turn_engine.py`、`tools.py`、`permission.py`、`workflow.py` 拆分 Protocol；每个 Protocol 不超过 15 个成员。
- [ ] 在 `src/tests/test_agent/application/` 使用内存 fake，依次为 `SessionService`、`CompactionService`、`ToolService`、`TurnService` 写 RED。
- [ ] 逐个创建 `application/{session_service,compaction_service,tool_service,turn_service}.py`；每完成一个垂直切片，就从对应 Graph coordinator/mixin 删除已迁移职责。
- [ ] `TurnService` 明确定义事件顺序、失败时持久化、cancel 和 guidance 行为；不直接发 UI concrete event。
- [ ] 为现有 memory、runtime UI、tools/permission 创建 infrastructure adapter；adapter 是唯一可导入 concrete service 的层。
- [ ] 验证：`./test.py --backend -- src/tests/test_agent/application src/tests/test_agent/graph/test_session_runtime_state.py src/tests/test_agent/graph/test_compaction_flow.py src/tests/test_agent/graph/test_execute_tools_guard.py -v`。

Acceptance：应用服务只使用 domain/ports；fake ports 可覆盖成功、失败、取消和恢复，不需要 Graph host。

### T4 — 封装 LangGraph 并删除类型环

- [ ] 为 `TurnEngine` contract 写 RED：给定 `TurnRequest + AgentRuntime` 返回 `TurnResult`，并覆盖 tool-call continuation、convergence、recursion limit。
- [ ] 创建 `src/voidx/agent/infrastructure/langgraph/{__init__,adapter,state_mapper,topology}.py` 和 `nodes/`；按节点职责迁移 `graph/core`、streaming、tool continuation 的执行逻辑。
- [ ] `LangGraphStateMapper` 是 `AgentRuntime` 与 LangGraph TypedDict 的唯一转换点。
- [ ] composition 只向 Application 注入 `TurnEngine`；Application 不导入 LangGraph。
- [ ] 删除 `graph/contracts.py` 对 concrete coordinator/runtime/runner 的类型引用；迁移完成后删除 `Graph*Host`。
- [ ] 更新 `src/tests/test_agent/test_dependency_cycles.py`，移除 5 模块类型 SCC 的 expected debt，完整类型图必须无 SCC。
- [ ] 验证：`./test.py --backend -- src/tests/test_agent/graph src/tests/test_agent/application src/tests/test_agent/test_dependency_cycles.py -v`。

Acceptance：运行时和完整类型导入图均无 SCC；LangGraph 仅存在于 infrastructure；执行 adapter 不管理 session/UI/config lifecycle。

### T5 — 迁移 Subagent、Slash 和组合根

- [ ] 为 `SubagentService` 写内存端口测试，覆盖 complete/incomplete/failed、并发隔离和 depth limit；迁移 `graph/subagent.py` 用例职责。
- [ ] 为 command parser/`CommandService` 写 RED，覆盖所有 `COMMANDS`、未知命令、async handler、model/session/permission/settings 更新。
- [ ] 创建 `presentation/slash/`；handler 只解析文本并调用 `CommandService`，不得访问 Graph 私有字段。
- [ ] 删除 `SlashHostAdapter` 和多继承 `SlashHandler`；保留命令名称、参数和用户可见结果。
- [ ] 创建 `composition.py` 和 `facade.py`；将 config、model、memory、UI、tools、permission、workflow、LangGraph adapter 的构造集中到 composition。
- [ ] 修改 `src/voidx/main.py` 只构造设置/启动参数并调用 facade；保留 Typer CLI 签名。
- [ ] `src/voidx/agent/graph/__init__.py` 可暂时 re-export facade 兼容旧导入，但内部不得使用。
- [ ] 新建 `src/tests/test_main.py`，覆盖 `_run_chat` 对 facade/composition 的调用、CLI 参数转发和启动失败 exit code。
- [ ] 验证：`./test.py --backend -- src/tests/test_agent/slash src/tests/test_agent/graph/test_parallel_subagents.py src/tests/test_agent/test_module_boundaries.py src/tests/test_main.py -v`。

Acceptance：slash/subagent 不持有 Graph host；`main.py` 不导入 `VoidXGraph`；所有依赖构造只有 composition 一处。

### T6 — 删除旧核心和兼容冗余

- [ ] 用 references/AST 确认后删除已无调用的 `Graph*Mixin`、coordinator、runner、proxy 方法和 `getattr(host, "_...")` compatibility code。
- [ ] 将仍有价值的纯函数迁移到 domain/application/infrastructure 对应职责文件，禁止复制后保留旧实现。
- [ ] 删除 Agent 内部 `agent.task_state` 引用；若项目外部兼容不要求该路径，删除文件，否则仅保留带明确 deprecation 的 re-export 和单测。
- [ ] 删除临时 `graph` facade；如需保持第三方导入兼容，只允许 `graph/__init__.py -> agent.facade` 单向 re-export，不保留旧实现。
- [ ] 增加冗余检查：禁止同名关键函数在新旧目录并存，禁止 dead compatibility allowlist。
- [ ] 运行 focused 后再运行 backend 全量：`./test.py --backend -- src/tests/test_agent src/tests/test_runtime src/tests/test_tools src/tests/test_ui src/tests/test_memory -v`，随后 `./test.py --backend`。

Acceptance：无 Graph Host/Mixin 私有状态架构；无跨包反向引用；旧实现已删除而非注释或复制保留。

## Compatibility Verification Matrix

| Surface | Proving command | Expected |
|---|---|---|
| CLI | `./test.py --backend -- src/tests/test_main.py src/tests/test_agent/slash -v` | 参数、命令和错误语义不变 |
| UI protocol | `./python.py scripts/export_ui_protocol_schema.py && git diff --exit-code -- frontend/src/rpc/protocol.schema.json` | schema 无非预期 diff |
| Runtime persistence | `./test.py --backend -- src/tests/test_memory src/tests/test_agent/graph/test_session_runtime_state.py -v` | 历史 fixture 可恢复且 round-trip 等价 |
| Agent behavior | `./test.py --backend -- src/tests/test_agent -v` | 全部通过 |
| Dependency boundaries | `./test.py --backend -- src/tests/test_agent/test_module_boundaries.py src/tests/test_agent/test_dependency_cycles.py -v` | 无 offender、无 SCC |
| Full backend | `./test.py --backend` | 全部通过 |

## Rollback Strategy

- 每个 Phase 单独提交；只允许回滚整个 Phase，不在 phase 中保留 feature flag 双核心。
- T1/T2 为机械边界迁移，可直接回滚 adapter 与导入变化。
- T3–T5 每次只切换一个用例 owner；切换提交必须同时删除旧 owner 的调用入口。
- 持久化 mapper 失败时回滚应用层切换，不修改数据库 schema 做补救。
- UI event 映射失败时回滚 event adapter，不改变 protocol schema 规避测试。

## Final Verification

所有阶段完成后按顺序执行并读取结果：

```bash
./test.py --backend -- src/tests/test_agent/test_module_boundaries.py src/tests/test_agent/test_dependency_cycles.py -v
./test.py --backend -- src/tests/test_agent src/tests/test_runtime src/tests/test_tools src/tests/test_ui src/tests/test_memory -v
./python.py scripts/export_ui_protocol_schema.py
git diff --exit-code -- frontend/src/rpc/protocol.schema.json
./test.py --backend
git diff --check
```

最终人工审查必须确认：

- `VoidXGraph` 已删除或只剩兼容 facade，不再作为全局可变容器。
- 没有组件通过 `host._private` 交换状态。
- 每个应用服务可用 fake ports 独立运行。
- CLI、UI schema、历史 session fixture 均有 fresh evidence。
- 只有全部验证通过后，才运行 `./scripts/archive.py docs/specs/agent-architecture-refactor-plan.md`。
