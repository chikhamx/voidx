> **Status: Done**

# 模块边界改进 — 技术设计文档

## Context

voidx 项目当前主模块职责划分和命名整体合理，但模块间仍存在循环依赖、跨层访问、高耦合等问题。以下内容按 2026-06-13 的代码现状校准：当前已有 `lsp/service.py`、`permission/service.py`、`skills/service.py`、`workflow/service.py`、`memory/service.py`、`llm/service.py`、`tools/service.py`，并已将部分跨层共享类型和常量下沉到 `runtime/`。随着功能增长，这些问题会导致：

- 改动一个模块需要理解多个模块的内部实现
- 测试隔离困难，mock 链过长
- 新增功能时难以确定代码归属

## Implementation Progress

当前已完成并由 `tests/test_module_boundaries.py` 守住的边界：

- `workflow ↔ skills`：`EXPLICIT_REF_RE` 已下沉到 `runtime/reference_tokens.py`；workflow 状态类型已下沉到 `workflow/types.py`
- `config → memory`：`config/settings.py` 已改走 `memory/service.py`
- `tools → permission`：`tools/bash.py` 已改走 `permission/service.py`
- `agent → llm/memory/permission/workflow/tools/skills/ui`：agent 侧已禁止访问已迁移模块的内部实现，统一走 `*.service`、`workflow.types`、`runtime.ui` / `runtime.ui_port`
- `ui → memory/tools/skills/agent`：已迁移可低风险下沉的展示类型和常量，包括 `TranscriptNodeRow`、`SkillRegistry`、`resolve_safe`、`TodoStatus`、`MAX_IMAGE_ATTACHMENT_BYTES`
- `runtime/memory/tools` 的 workflow 类型依赖：只读类型导入已改走 `workflow.types`，状态推进和 workflow policy 调用改走 `workflow.service`

仍未一次性收紧的边界：

- `agent` 仍直接使用 `llm.compaction`、`llm.instruction`、`llm.usage`、`llm.message_markers`、`llm.catalog` 等已存在的 LLM 子服务/展示 helper；后续若要进一步收口，应扩展 `llm/service.py` 或拆出更明确的 `llm/types.py` / `llm/formatting.py`
- `ui` 仍直接使用 `llm.usage` 的展示格式化函数和 `UsageStats`；这是 UI 展示层和 LLM 统计 DTO 的边界，建议单独拆 `runtime/usage.py` 或 `llm/usage_service.py`
- `runtime/ui_port.py` 仍在 `TYPE_CHECKING` 下引用 UI event/tree 类型；这属于协议类型耦合，暂未强行拆分

## Goals and Non-Goals

### Goals

- 消除 `workflow ↔ skills` 循环依赖
- 修复 `config → memory` 跨层访问
- 降低 `agent` 模块对其他模块的耦合度
- 为 `ui` 层建立与业务逻辑的隔离层
- 解耦 `tools` 对 `permission` 实现的直接依赖

### Non-Goals

- 不重构模块目录结构（当前划分合理）
- 不改变已有的 Protocol 接口签名
- 不涉及 `mcp_servers → tools` 的依赖（内置服务器复用工具实现是合理的）

## Architecture

### 当前依赖图（问题标注）

```
                    ┌──────────────────────────────────┐
                    │            agent (god module)     │
                    │  依赖全部 12 个模块，7 个重度依赖  │
                    └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─┘
                       │  │  │  │  │  │  │  │  │  │  │  │
          ┌────────────┘  │  │  │  │  │  │  │  │  │  │  └─────────┐
          ▼               │  │  │  │  │  │  │  │  │  │            ▼
       ┌─────┐         ┌──┘  │  │  │  │  │  │  │  │  └──┐      ┌────┐
       │ llm │◄────────┤     │  │  │  │  │  │  │  │     │      │ ui │
       └──┬──┘         │     │  │  │  │  │  │  │  │     │      └─┬──┘
          │            ▼     │  │  │  │  │  │  │  ▼     ▼        │
          │     ┌─────────┐  │  │  │  │  │  │  └──────┐ │        │
          │     │ memory  │  │  │  │  │  │  │         │ │        │
          │     └────┬────┘  │  │  │  │  │  │         │ │        │
          │          │       │  │  │  │  │  │         │ │        │
          │     ┌────▼────┐  │  │  │  │  │  │         │ │        │
          │     │ config  │──┼──┼──┼──┼──┼──┼─►(跨层) │ │        │
          │     └─────────┘  │  │  │  │  │  │         │ │        │
          │                  ▼  │  │  │  │  ▼         │ │        │
          │            ┌──────┐ │  │  │  │ ┌───────┐  │ │        │
          │            │ tools│─┼──┼──┼──┼►│skills │◄─┼─┤        │
          │            └──┬───┘ │  │  │  │ └───┬───┘  │ │        │
          │               │     │  │  │  │     │      │ │        │
          │               ▼     │  │  │  │     ▼      │ │        │
          │          ┌────────┐ │  │  │  │ ┌────────┐ │ │        │
          │          │permission│ │  │  │  │ │workflow│◄┼─┤        │
          │          └────────┘ │  │  │  │ └───┬────┘ │ │        │
          │                     │  │  │  │     │      │ │        │
          │                     │  │  │  │     └──►(循环)        │
          │                     │  │  │  │            │ │        │
          ▼                     ▼  ▼  ▼  ▼            ▼ ▼        ▼
       ┌─────┐  ┌────┐  ┌─────┐  ┌───┐  ┌──────┐  ┌──────┐  ┌────┐
       │ mcp │  │lsp │  │mcp_ │  │run│  │      │  │      │  │    │
       │     │  │    │  │srv  │  │tim│  │      │  │      │  │    │
       └─────┘  └────┘  └─────┘  └───┘  └──────┘  └──────┘  └────┘
```

### 问题 1：workflow ↔ skills 循环依赖

**现状**：
- `skills/policy.py` → `workflow.policy`（获取 `workflow_denied_tools`、`workflow_gate` 等）
- `skills/runtime.py` 已改为从 `workflow.types` 获取 `WorkflowRunState`、`WorkflowRunStatus` 等纯类型
- `workflow/service.py` 已改为从 `runtime.reference_tokens` 获取 `EXPLICIT_REF_RE`

**方案：拆成两个稳定公共边界**

`workflow` 和 `skills` 的核心循环不是因为 workflow state 类型本身，而是因为 explicit reference 语法目前放在 `skills.schema`，却被 workflow 选择逻辑复用。因此先拆 reference token，再拆 workflow types：

```
runtime/
├── reference_tokens.py  ← 新文件，存放 EXPLICIT_REF_RE 等跨功能引用语法

workflow/
├── types.py          ← 新文件，存放 WorkflowRunState, WorkflowRunStatus 等 workflow 数据类型
├── runtime.py        ← 保留状态推进逻辑，从 types 重新导出类型
├── service.py        ← 从 runtime.reference_tokens 导入 EXPLICIT_REF_RE
└── ...

skills/
├── schema.py         ← 从 runtime.reference_tokens 重新导出 EXPLICIT_REF_RE（兼容旧导入）
├── policy.py         ← 短期保留 workflow.policy 兼容别名
├── runtime.py        ← 若只需要类型，则改从 workflow.types 导入
└── ...
```

**迁移步骤**：
1. 创建 `runtime/reference_tokens.py`，移动 `EXPLICIT_REF_RE`
2. `skills/schema.py` 从 `runtime.reference_tokens` 重新导出 `EXPLICIT_REF_RE`，保持兼容
3. `workflow/service.py` 改为从 `runtime.reference_tokens` 导入 `EXPLICIT_REF_RE`
4. 创建 `workflow/types.py`，移动纯 workflow runtime 数据类型；`workflow/runtime.py` 继续提供 `advance_workflow_states()`、transition helpers，并重新导出类型
5. `skills/runtime.py` 若只使用类型，改为从 `workflow.types` 导入
6. 验证无循环后，再考虑是否清理 `skills/policy.py`、`skills/runtime.py` 这些兼容模块

### 问题 2：config → memory 跨层访问

**现状**：
- `config/settings.py` 已改为导入 `memory.service` 的 `list_model_profiles_async`、`get_model_profile_async`、`save_model_profile_async`、`delete_model_profile_async`、`ModelProfileRow`
- `memory/service.py` 同时作为 agent/UI 使用的 session、runtime state、transcript facade

**方案：通过 service 层间接访问**

```
# 当前
config/settings.py → memory.model_profiles (直接访问持久化层)

# 目标
config/settings.py → memory/service.py → memory.model_profiles (通过 service 层)
```

**迁移步骤**：
1. 新增 `memory/service.py`，先作为薄 facade 暴露 model profile 的查询、保存、删除接口
2. `config/settings.py` 改为从 `memory.service` 导入，不再直接访问 `memory.model_profiles`
3. 初期允许 `memory.service` 重新导出 `ModelProfileRow`，避免一次性改动 config profile 转换逻辑
4. 后续如需进一步收紧，再把 config-facing DTO 从持久化 row 中拆出来

### 问题 3：agent 模块过度耦合

**现状**：
- `agent` 依赖全部 12 个其他模块，其中 7 个是重度依赖
- 大量胶水代码分散在 `agent/graph/core.py`、`run_loop.py`、`turn_runner.py`、`tool_executor.py`、`session_runtime.py`、`subagent.py`、`compaction_coordinator.py` 等文件
- 当前已移除 `agent` 对 `llm.provider`、`memory` 内部持久化模块、`workflow.runtime/context/policy/auto_advance`、`tools.base/registry/task_tracker/agent`、`skills.context/references/registry`、具体 `voidx.ui.*` 实现的直接导入
- 剩余直接导入主要是 `llm.compaction`、`llm.instruction`、`llm.usage`、`llm.message_markers`、`llm.catalog` 这类 LLM 子服务或展示 helper

**方案：引入 facade service 层，将协调逻辑下沉**

```
# 当前：agent 直接操作各模块内部
agent/graph/*.py → llm.provider, llm.compaction, memory.session, ui.gateway, ...

# 目标：agent 通过各模块的 service 层交互
agent/graph/*.py → llm.service, memory.service, permission.service, runtime.ui_port, ...
```

**原则**：
- 跨模块业务调用优先走 `service.py` 或明确的 facade
- 纯类型、schema、protocol、常量可作为稳定公共边界，不强制塞进 `service.py`
- `agent/graph` 不直接导入其他模块的内部实现文件
- 模块内部子模块之间的导入不受限制

**迁移步骤**：
1. 审计整个 `agent/graph/` 的所有外部导入，列出每个导入的来源模块和用途
2. 先迁移低风险 facade：`memory/service.py`、`llm/service.py`
3. 保留 `runtime/ui_port.py` 作为 agent-facing UI protocol，但把具体 UI 构造和 bootstrap 继续往 `runtime/ui.py` 或专门 facade 下沉
4. 逐步将 `agent/graph` 的业务调用从内部子模块迁移到 service/facade/type/protocol 公共边界
5. 每迁移一个模块，运行测试验证

### 问题 4：ui 层与业务逻辑耦合

**现状**：
- `ui` 直接导入 `agent`、`llm`、`memory`、`tools`、`skills`
- `runtime/ui_port.py` 已定义 agent-facing UI Protocol，位置合理，当前不移动
- agent → UI 方向已改为 `runtime.ui_port` / `runtime.ui` facade
- UI 内部已移除低风险业务直连：`ui/transcript.py` → `memory.service`，`ui/session.py` → `tools.service`，`ui/tools/clipboard_image.py` → `runtime.attachments`，`ui/output/events/schema.py` → `runtime.todo`，skill picker/panel → `skills.service`
- 仍存在 UI 展示层对 `llm.usage` 的格式化/统计 DTO 依赖，后续应拆为更稳定的 usage DTO 或 formatting facade

**方案：把 UI 边界分成两个方向治理**

```
# 当前
agent/graph → ui concrete implementations
ui/ → agent, llm, memory, tools, skills

# 目标
agent/graph → runtime.ui_port / runtime.ui facade → ui concrete implementations
ui/ → runtime protocols/types/facades 或 ui-owned DTO
```

**迁移步骤**：
1. 保留 `runtime/ui_port.py`，明确它是 agent → UI 的 runtime 边界
2. 优先移除 `agent/graph` 对具体 UI 实现的导入，把 TUI/gateway/bootstrap 创建下沉到 `runtime/ui.py` 或专门 facade
3. 审计 `ui/` 中所有业务模块导入，区分三类：展示格式化 helper、协议 payload 类型、真正业务调用
4. 对展示所需的轻量类型，迁到 `runtime` 或 `ui` 自有 DTO；对真正业务调用，改为 runtime facade 或启动时注入
5. 每类迁移完成后补模块边界测试，避免新增反向依赖

### 问题 5：tools 直接依赖 permission 实现

**现状**：
- `tools/bash.py` 直接操作 `permission.engine` 和 `permission.sandbox`
- `permission/service.py` 已存在，但尚未暴露 bash 工具需要的 `is_safe_bash` / `check_sandbox_bash` facade

**方案：通过 permission service 模块暴露 bash-facing facade**

```
# 当前
tools/bash.py → permission.engine, permission.sandbox (直接依赖实现)

# 目标
tools/bash.py → permission/service.py (通过公共接口)
```

**迁移步骤**：
1. 在 `permission/service.py` 暴露 bash 工具需要的纯 facade，例如 `is_safe_bash_command()`、`bash_sandbox_denial()`
2. `tools/bash.py` 改为从 `permission/service` 导入
3. 保持命令执行仍在 `tools/bash.py`，permission service 只负责权限和沙箱判断，不接管 subprocess 生命周期

## Data Model

本次重构不涉及数据模型变更。主要变更在模块间的导入关系和接口定义。

## API Contract

### workflow/types.py（新增）

```python
# workflow 运行时数据类型；不包含 skills 引用语法
class WorkflowRunState: ...
class WorkflowRunStatus: ...
class WorkflowActivationSource: ...
class WorkflowStateEvent: ...
```

### runtime/reference_tokens.py（新增）

```python
EXPLICIT_REF_RE: re.Pattern
```

### 各模块 service.py 扩展

| 模块 | 新增接口 | 说明 |
|------|---------|------|
| `runtime/reference_tokens.py` | `EXPLICIT_REF_RE` | 替代 workflow 从 skills.schema 读取共享引用语法 |
| `memory/service.py` | `list_model_profiles_async()`, `get_model_profile_async()`, `save_model_profile_async()`, `delete_model_profile_async()` | 替代 config 直接访问 model_profiles |
| `llm/service.py` | `create_chat_model()`, `resolve_protocol()`, `get_context_limit()` 等薄 facade；后续再收 compaction/usage | agent 不再直接导入 provider 内部实现 |
| `permission/service.py` | `is_safe_bash_command()`, `bash_sandbox_denial()` | tools 不再直接导入 engine/sandbox |
| `tools/service.py` | `ToolRegistry`, `ToolContext`, `ToolResult`, `TaskTracker`, `AgentTool` 等薄 facade | agent 不再直接导入 tools 内部实现文件 |
| `skills/service.py` | `SkillRegistry`, skill context helpers, `skill_reference_message()` | agent/UI 不再直接导入 skills registry/context/reference 实现 |
| `workflow/service.py` | `advance_workflow_states()`, `auto_advance_events()`, workflow policy/context helpers | agent/tools 不再直接导入 workflow runtime/policy/context |
| `runtime/todo.py` | `TodoStatus` | UI event schema 不再依赖 `tools.todo` |
| `runtime/attachments.py` | `MAX_IMAGE_ATTACHMENT_BYTES` | UI clipboard image helper 不再依赖 `agent.attachments` |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 迁移后循环依赖未消除 | CI 中添加 import 循环检测（自定义脚本优先；若引入第三方工具需单独评估依赖成本） |
| service 层接口不完整 | 先补充接口再迁移，不跳步 |
| 迁移导致运行时 ImportError | 每步迁移后运行全量测试 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 先提取 `runtime/reference_tokens.py`，再提取 `workflow/types.py` | 只把所有共享内容放进 `workflow/types.py` | `EXPLICIT_REF_RE` 是跨功能引用语法，不属于 workflow 领域类型 |
| 通过 service 层解耦 | 使用事件总线 | 引入事件总线过重，service 层更简单直接 |
| UI 通过 Protocol 隔离 | 完全拆分前后端 | 当前架构不需要，Protocol 足够 |
| 逐步迁移而非一次性重构 | 一次性重写所有导入 | 逐步迁移风险更低，每步可验证 |

## Resolved Questions

### Q1: `runtime/ui_port.py` 的 Protocol 定义应该留在 `runtime` 还是移到 `ui/protocol/`？

**结论：留在 `runtime/`，不移动。**

- 这些 Protocol（`AgentUiPort`、`AgentDock`、`AgentEventBus` 等）全部是 agent → UI 方向的接口，消费者只有 `agent/graph/` 下的 4 个文件
- `ui/protocol/` 目前存的是前端通信协议（envelope、requests、schema），职责不同
- `runtime/` 的定位是"共享运行时"，放 agent 和 ui 之间的 Protocol 接口合理
- 如果移到 `ui/protocol/`，会让 `ui` 模块变成 agent 的依赖方向，依赖方向反了

### Q2: `agent` 的 service 层下沉是否需要同步调整 `agent/graph/` 的子模块结构？

**结论：不需要调整 agent 子模块结构，只改导入路径。**

- `agent/graph/` 内部的 mixin 拆分（`RunLoopMixin`、`CompactionMixin`、`PermissionMixin` 等）已经很清晰
- 真正的问题是跨模块导入走了内部子模块（如 `llm.provider` 而非 `llm.service`），以及启动/展示路径直接依赖具体 UI 实现
- 调整 agent 子模块结构风险高（涉及 LangGraph 状态机绑定），收益低
- agent 侧先改公共边界导入路径，不重排 graph mixin/runner 结构

### Q3: 是否需要在 CI 中添加自动化的模块依赖检测？

**结论：需要加，放在 `tests/test_module_boundaries.py`。**

分三个层次逐步收紧：
1. **循环依赖检测** — 禁止新增循环 import
2. **跨层访问检测** — 禁止 config 直接访问 memory 内部子模块等
3. **内部子模块访问检测** — 跨模块应走 service 层，不应直接导入内部子模块

定义合法依赖方向白名单（`ALLOWED_DEPS`），CI 中作为门禁执行。

建议首批门禁只覆盖已完成迁移的边界，避免一次性把当前已知债务全部变成失败测试：

1. 禁止 `workflow/service.py` 导入 `voidx.skills.*`
2. 禁止 `config/settings.py` 导入 `voidx.memory.model_profiles`
3. 禁止 `tools/bash.py` 导入 `voidx.permission.engine` / `voidx.permission.sandbox`
4. 后续每迁移一个 `agent/graph` 或 `ui` 边界，再把对应规则加入 `tests/test_module_boundaries.py`
