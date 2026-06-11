# 模块边界改进 — 技术设计文档

## Context

voidx 项目当前有 13 个主模块，职责划分和命名整体合理，但模块间存在循环依赖、跨层访问、高耦合等问题。随着功能增长，这些问题会导致：

- 改动一个模块需要理解多个模块的内部实现
- 测试隔离困难，mock 链过长
- 新增功能时难以确定代码归属

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
- `skills/runtime.py` → `workflow.runtime`（获取 `WorkflowRunState`、`WorkflowRunStatus`）
- `workflow/service.py` → `skills.schema`（获取 `EXPLICIT_REF_RE`，通过 lazy import 规避）

**方案：提取共享类型到 `workflow.types`**

```
workflow/
├── types.py          ← 新文件，存放 WorkflowRunState, WorkflowRunStatus, EXPLICIT_REF_RE 等
├── policy.py         ← 从 types 导入，不再被 skills 反向依赖
├── runtime.py        ← 从 types 导入
├── service.py        ← 从 types 导入 EXPLICIT_REF_RE，无需 lazy import
└── ...

skills/
├── policy.py         ← 从 workflow.types 导入，不再依赖 workflow.policy
├── runtime.py        ← 从 workflow.types 导入，不再依赖 workflow.runtime
└── ...
```

**迁移步骤**：
1. 创建 `workflow/types.py`，将 `WorkflowRunState`、`WorkflowRunStatus`、`EXPLICIT_REF_RE` 等纯数据类型/常量移入
2. 更新 `workflow/policy.py`、`workflow/runtime.py` 从 `types` 重新导出（保持向后兼容）
3. 更新 `skills/policy.py`、`skills/runtime.py` 改为从 `workflow.types` 导入
4. 更新 `workflow/service.py` 改为从 `workflow.types` 导入 `EXPLICIT_REF_RE`，移除 lazy import
5. 验证无循环后，清理 `workflow/policy.py` 和 `workflow/runtime.py` 中的重新导出

### 问题 2：config → memory 跨层访问

**现状**：
- `config/settings.py` 直接导入 `memory.model_profiles` 的 `list_model_profiles_async`、`ModelProfileRow`、`save_model_profile_async`

**方案：通过 service 层间接访问**

```
# 当前
config/settings.py → memory.model_profiles (直接访问持久化层)

# 目标
config/settings.py → memory/service.py → memory.model_profiles (通过 service 层)
```

**迁移步骤**：
1. 在 `memory/service.py` 中暴露 model profile 的查询和保存接口（如果尚未暴露）
2. `config/settings.py` 改为从 `memory.service` 导入，不再直接访问 `memory.model_profiles`
3. 确保 `memory/service.py` 的接口签名与当前使用方式兼容

### 问题 3：agent 模块过度耦合

**现状**：
- `agent` 依赖全部 12 个其他模块，其中 7 个是重度依赖
- 大量胶水代码集中在 `agent/graph/core.py`

**方案：引入 facade service 层，将协调逻辑下沉**

```
# 当前：agent 直接操作各模块内部
agent/graph/core.py → llm.provider, llm.compaction, memory.session, ...

# 目标：agent 通过各模块的 service 层交互
agent/graph/core.py → llm/service, memory/service, permission/service, ...
```

**原则**：
- 每个模块的 `service.py` 是该模块唯一的公共入口
- `agent` 只导入 `xxx/service.py`，不导入模块内部子文件
- 模块内部子模块之间的导入不受限制

**迁移步骤**：
1. 审计 `agent/graph/core.py` 的所有外部导入，列出每个导入的来源模块
2. 为缺少 service 层的模块补充 `service.py`（如 `llm/service.py`）
3. 逐步将 `agent` 的导入从内部子模块迁移到 service 层
4. 每迁移一个模块，运行测试验证

### 问题 4：ui 层与业务逻辑耦合

**现状**：
- `ui` 直接导入 `agent`、`llm`、`memory`、`tools`、`skills`
- `runtime/ui_port.py` 定义了 UI 类型 Protocol，但放在了 `runtime` 模块

**方案：UI 只通过 runtime Protocol 交互**

```
# 当前
ui/ → agent, llm, memory, tools, skills (直接导入业务模块)

# 目标
ui/ → runtime (Protocol 接口) → agent, llm, memory, ... (运行时注入)
```

**迁移步骤**：
1. 将 `runtime/ui_port.py` 中的 UI Protocol 定义移到 `ui/protocol/` 或保留在 `runtime` 但明确其桥接角色
2. 审计 `ui/` 中所有对业务模块的直接导入
3. 为每个直接导入定义对应的 Protocol 接口（在 `runtime/` 中）
4. 在 agent 启动时将实现注入到 UI 层
5. 逐步替换直接导入为 Protocol 调用

### 问题 5：tools 直接依赖 permission 实现

**现状**：
- `tools/bash.py` 直接操作 `permission.engine` 和 `permission.sandbox`

**方案：通过 permission service 层或注入解耦**

```
# 当前
tools/bash.py → permission.engine, permission.sandbox (直接依赖实现)

# 目标
tools/bash.py → permission/service.py (通过公共接口)
```

**迁移步骤**：
1. 确保 `permission/service.py` 暴露了 bash 工具需要的权限检查和沙箱执行接口
2. `tools/bash.py` 改为从 `permission/service` 导入
3. 如果 service 层缺少必要接口，先补充再迁移

## Data Model

本次重构不涉及数据模型变更。主要变更在模块间的导入关系和接口定义。

## API Contract

### workflow/types.py（新增）

```python
# 纯数据类型和常量，无业务逻辑依赖
class WorkflowRunState: ...
class WorkflowRunStatus: ...
EXPLICIT_REF_RE: re.Pattern
```

### 各模块 service.py 扩展

| 模块 | 新增接口 | 说明 |
|------|---------|------|
| `memory/service.py` | `get_model_profiles()`, `save_model_profile()` | 替代 config 直接访问 model_profiles |
| `llm/service.py` | 统一 LLM 调用入口 | agent 不再直接导入 provider/compaction |
| `permission/service.py` | `check_permission()`, `run_in_sandbox()` | tools 不再直接导入 engine/sandbox |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 迁移后循环依赖未消除 | CI 中添加 import 循环检测（`import-graph` 或自定义脚本） |
| service 层接口不完整 | 先补充接口再迁移，不跳步 |
| 迁移导致运行时 ImportError | 每步迁移后运行全量测试 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 提取 `workflow/types.py` 消除循环 | 合并 workflow 和 skills 为一个模块 | 两个模块职责不同，合并会模糊边界 |
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
- 真正的问题是跨模块导入走了内部子模块（如 `llm.provider` 而非 `llm.service`），这是其他模块的问题
- 调整 agent 子模块结构风险高（涉及 LangGraph 状态机绑定），收益低
- agent 侧只需改导入路径（从 `llm.provider` 改为 `llm.service`），风险很低

### Q3: 是否需要在 CI 中添加自动化的模块依赖检测？

**结论：需要加，放在 `tests/test_module_boundaries.py`。**

分三个层次逐步收紧：
1. **循环依赖检测** — 禁止新增循环 import
2. **跨层访问检测** — 禁止 config 直接访问 memory 内部子模块等
3. **内部子模块访问检测** — 跨模块应走 service 层，不应直接导入内部子模块

定义合法依赖方向白名单（`ALLOWED_DEPS`），CI 中作为门禁执行。
