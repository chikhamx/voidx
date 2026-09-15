# 工具调用权限审批归一化技术规格文档

> **Status: Done** — Archived on 2026-09-16.

- **日期**：2026-09-14
- **状态**：方案已修订，待最终确认（Revised after Subagent Independent Review）
- **读者**：架构维护者、后端核心开发者与实施代理
- **涉及范围**：`src/voidx/tooling/`、`src/voidx/agent/adapters/langgraph/runtime/` 及相关测试套件

---

## 1. 背景与根因剖析

### 1.1 现状：“双轨制”审批架构
当前系统中对工具调用（尤其是涉及工作区外部路径的读写操作）存在两套相互独立的拦截与人机交互（HITL）审批链路：

```
[LLM 产出 Tool Calls]
        │
        ▼
[第一轨：调度层前置拦截 (PermissionFlow._authorize_tool_calls)]
        │ ── 检测到外部路径 (resolve_access 返回 defer)
        │ ── 弹出 UI 提示 1："Allow tools: read / write?"
        │ ── 用户授权 (例如选择 "Allow once")
        │ ── 向 PermissionService 注入临时 AccessGrant (runtime)
        ▼
[工具开始分发执行 (ToolExecutorAdapter)]
        ▼
[第二轨：工具内部底层门禁 (authorized_path / FileReadTool / WriteTool / ManageTool)]
        │ ── 再次调用 resolve_access 校验路径
        │ ── 若未命中或状态不符，底层再次调用 interaction.request()
        │ ── 弹出 UI 提示 2："Read/Write file outside workspace?"
        ▼
[文件系统读写 / 返回结果]
```

### 1.2 二次审批与体验割裂的具体根因

1. **时序与校验逻辑倒置（`resolve_access` 检查缺陷）**
   - **源码位置**：`src/voidx/tooling/policy/filesystem/grants.py:60-71`
   - **问题**：在 `resolve_access` 中，`require_exists`（针对读、追加写、重命名等）和 `allow_missing_write_file` 的存在性检查被置于 `_matches_grant` **之前**。
   - **后果**：当 Agent 尝试读取一个尚不存在的外部文件时，第一轨通过用户审批写入了 Grant；但当底层工具执行 `authorized_path(require_exists=True)` 时，由于磁盘文件不存在，`resolve_access` 提前返回 `defer`（绕过了已注入的 Grant），导致底层门禁认为该操作“仍需审批”，从而向用户触发了第二次弹窗。

2. **双层租约冲突与提前释放（Dual Lease Lifecycle Conflict）**
   - **源码位置**（行号以 `executor.py` / `helpers.py` / `permission_service.py` 中符号为准，随代码演进可能漂移）：
     - `src/voidx/tooling/application/permission_service.py` 的 `PermissionService.execution_lease_for_tool`（finally 块调用 `_clear_runtime_grants()`）
     - `src/voidx/agent/adapters/langgraph/runtime/tool_executor/executor.py` 的单工具租约嵌套点（`lease_factory = getattr(host._permission, "execution_lease_for_tool", None)` → `async with lease_factory(tid):`）
     - `src/voidx/agent/adapters/langgraph/runtime/tool_executor/helpers.py` 的 `_execute_approved_batch`（批次租约 `async with lease_factory("approved_batch"):`）
   - **问题**：`helpers.py` 的 `_execute_approved_batch` 在外层虽然有批次租约 `lease_factory("approved_batch")`，但在 `executor.py` 中每个工具执行时又独立嵌套调用了 `lease_factory(tid)`。当单工具执行完毕退出 contextmanager 时，其 `finally` 块显式调用了 `_clear_runtime_grants()`。
   - **后果**：当同一批次（Batch）内包含多个外部路径调用（例如模型同时发起 `read(ext_a)` 和 `read(ext_b)`），执行完 `ext_a` 后，单个工具的 lease 退出把整个会话的所有 `runtime_grants` 清除。随后 `ext_b` 开始执行时 Grant 已被抹去，底层门禁再次拦截弹窗。

3. **双重交互与选项定义不一致**
   - 第一轨（`PermissionFlow`）定义的选项：`allow`, `session_file`, `session_dir`, `persistent_file`, `persistent_dir`, `deny`。
   - 第二轨（`authorized_path`）定义的选项：`allow`（Once）, `session_file`（Session file）, `session_dir`（Session dir）...
   - 两处文案、交互事件（`PermissionPromptShown` vs `UserInteraction`）各走一套，维护成本高且前端表现不统一。

---

## 2. 目标与非目标 (Goals & Non-Goals)

### 2.1 目标 (Goals)
1. **单一事实来源（Single Source of Decision）**：所有工具调用权限与路径越权审批，**100% 收拢至调度层 `PermissionFlow`**，用户在工具执行前仅需审批一次。
2. **底层门禁纯化（Enforcement-Only Gatekeeper）**：底层 `authorized_path` 严格作为断言门禁（Gatekeeper）。未获得授权的外部路径访问直接返回拒绝错误（Fail-Closed），**彻底移除工具内部发起 UI 交互（`interaction.request`）以及配套的 lock/writer 等死代码**。
3. **修复 Grant 判定时序**：在 `resolve_access` 中，**优先匹配 Grants**，授权通过后再由工具自身判定文件是否存在并报告正常业务错误（如 "File not found"），不再因文件不存在退化为权限待审批（`defer`）。
4. **统一批次租约生命周期**：移除单工具级别的 Grant 提前清理，`runtime_grants` 的有效期严格与整段 Approved Batch 对齐，整批完成或异常退出时通过外层租约统一释放。
5. **统一选项与数据模型**：抽取全局统一的外部路径授权选项定义，打通 CLI/TUI、Web/Desktop Gateway 以及 AI 自动审批通道。

### 2.2 非目标 (Non-Goals)
1. 不改变现有的高层权限模式（`read_only`, `safe`, `ai_approval`, `project_trusted`, `full_access`）的分层定义。
2. 不放宽沙箱防护边界：跨工作区路径仍然受到严格保护，非工作区路径在未获授权时坚决拒绝。
3. 不变更已有的 `checkpoint` 计划审批工具（Plan Checkpoint 属于 Agent 任务编排层，与工具执行权限层正交）。

### 2.3 已知限制（评审记录）
- **子代理外部路径审批为既有缺口，本改造不修复**：子代理通过 flow 层审批外部路径时，Grant 经 `execution.py` 的 `authorize` 闭包写入**父** `PermissionService`；而子代理底层 `AuthorizationRuntime` 读取的是 spawn 时捕获的**只读快照**（`subagent.py` 的 `permission_snapshot.get_access_grants()`），新注入的 Grant 不可见。审批"100% 收拢至调度层"后该缺口仍然存在（表现为：子代理审批通过后仍被底层 Fail-Closed 拒绝）。这是既有行为、非本改动引入；如子代理确实需要外部路径授权，应在 spawn 前由父代理获批或另行立项解决。

---

## 3. 总体架构设计

### 3.1 归一化后的数据流与职责边界

```
                   [LLM 产出 Tool Calls]
                             │
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │        LangGraph 运行时拦截 (PermissionFlow)           │
 │  - 收集所有调用意图 (工具名/参数/风险/外部路径)        │
 │  - 预授权检查 (Preset / Session Rules / Grants)        │
 │  - 如需审批：统一弹出【唯一审批窗口】                 │
 │  - 用户做出决策：单次 / 本会话 / 持久化 / 拒绝        │
 │  - 提交 Grant 更新到 PermissionService                 │
 └───────────────────────────┬────────────────────────────┘
                             │ (批次内工具全部具备有效 Grant)
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │        工具执行器 (ToolExecutorAdapter)                │
 │  - 外层管理整批 Approved Batch 的 Execution Lease      │
 │  - 批次内工具并发/串行安全执行                         │
 │  - 批次执行全部结束才统一清空当前轮 runtime_grants     │
 └───────────────────────────┬────────────────────────────┘
                             │
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │        底层工具沙箱门禁 (authorized_path)              │
 │  - 纯门禁：仅查验 Grant (resolve_access: allow / deny) │
 │  - 命中 Grant：放行并交由工具函数执行                  │
 │  - 未命中 Grant：Fail-Closed 拒绝，不弹窗！            │
 └────────────────────────────────────────────────────────┘
```

---

## 4. 详细技术规范与改造点

### 4.1 核心改动 1：`resolve_access` 判定顺序校正
- **文件**：`src/voidx/tooling/policy/filesystem/grants.py`
- **规范**：
  1. 工作区路径（`is_workspace_path`）最先放行。
  2. 若不是工作区路径，先检查 `grants.permission_state_ready`。
  3. **关键调整**：立即执行 `_matches_grant(...)` 检查。
     - 若命中已有 Grant：直接返回 `AccessResolution("allow", intent=intent_with_grant)`。
     - 若未命中 Grant：再根据 `require_exists` / `allow_missing_write_file` 等条件生成 `defer` 或 `deny` 决策。
  4. 影响：已获授权的路径，即便文件在磁盘上尚未建立（写场景）或确实不存在（读场景），都能顺利拿到 `allow`，把控制权交给具体工具报“文件不存在”，绝不再二次 defer。

### 4.2 核心改动 2：底层 `authorized_path` 纯门禁化与死代码剥离
- **文件**：`src/voidx/tooling/application/authorization.py`、`src/voidx/tooling/application/execution.py`
- **规范**：
  1. 彻底删除 `authorized_path` 内部的：
     - `authorization.interaction.request(...)` 交互逻辑与 options 列表。
     - `authorization.target_locker` 路径锁逻辑（调度层已统筹管控）。
     - `authorization.grant_writer` 提权写入逻辑。
  2. 当 `resolve_access(...)` 返回 `action != "allow"` 时：
     - 直接视为**未授权**并返回 `ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True, "unauthorized": True})`。
  3. 交互删除后随之成为死代码、须一并移除的辅助函数（当前仅 `authorized_path` 引用，已 grep 核实）：
     - `_grant_choice`
     - `_release_lock`
     - `_call_add_grant`
     - `_approval_precondition`
  4. 清理 `AuthorizationRuntime`（`src/voidx/tooling/application/execution.py`）中仅服务于底层交互的冗余字段，使底层成为纯净的授权状态查询接口：
     - `interaction`
     - `grant_writer`
     - `target_locker`
     - `execution_lease_factory`（无任何消费方，已 grep 核实；注意 `executor.py` 的执行路径直接持有 `permission.execution_lease_for_tool`，不读 `AuthorizationRuntime` 的该字段，移除不影响执行）
  5. 同步移除 `executor.py` 构造 `AuthorizationRuntime` 时的对应注入点：`interaction=CallbackInteractionPort(_make_interact_callback(...))`、`grant_writer=permission.add_grant`、`target_locker=permission.acquire_grant_targets`、`execution_lease_factory=permission.execution_lease_for_tool`，并删除 `CallbackInteractionPort` 类与孤儿模块 `src/voidx/tooling/ports/interaction.py`。
     - **修正（实施核实）**：`_make_interact_callback` 函数本身**保留**——它同时注入 `AgentToolRuntime.interaction`，服务于 clarify/checkpoint/loop/goal 等 agent 级工具交互，不属于本次死代码范围。

### 4.3 核心改动 3：批次租约与 Grant 释放生命周期治理
- **文件**：
  - `src/voidx/tooling/application/permission_service.py`
  - `src/voidx/agent/adapters/langgraph/runtime/tool_executor/executor.py`
  - `src/voidx/agent/adapters/langgraph/runtime/tool_executor/helpers.py`
- **规范**：
  1. 移除 `executor.py` 中单工具级别的 `async with lease_factory(tid):` 嵌套（围绕 `run_authorized_tool` 的调用点），避免其 `finally` 提前调用 `_clear_runtime_grants()`。
  2. 保留并巩固 `helpers.py` 中 `_execute_approved_batch` 末尾的批次租约 `async with lease_factory("approved_batch"):`，确保其包裹整段批次执行。
  3. 在整批工具执行完毕（无论并发还是串行）退出该批次租约后，再统一执行 `_clear_runtime_grants()`，彻底消除同批多工具调用时后续工具的授权提前失效问题。
  4. `PermissionService.execution_lease_for_tool` 的"租约退出即清理 runtime grants"语义保持不变（测试仍直接调用它），但移除执行路径的单工具嵌套后，清理时机仅由批次租约触发。

### 4.4 核心改动 4：统一外部路径授权选项定义
- **文件**：`src/voidx/tooling/domain/grants.py`
- **规范**：
  1. 定义标准枚举：
     ```python
     class PathGrantChoice(str, Enum):
         ONCE = "allow"
         SESSION_FILE = "session_file"
         SESSION_DIR = "session_dir"
         PERSISTENT_FILE = "persistent_file"
         PERSISTENT_DIR = "persistent_dir"
         DENY = "deny"
     ```
  2. `PermissionFlow._path_grant_choices` 使用统一标准选项生成规范的 label、value 与 description。
  3. 前端/网关 Wire Values 映射统一。

---

## 5. 测试套件迁移方案 (Test Migration Plan)

由于历史版本中有测试依赖于模拟底层 `authorized_path` 弹窗来测试交互，必须分类进行迁移与重构，避免测试套件断崖式报错：

### 5.1 纯门禁校验类测试（断言 Fail-Closed）
- **涉及文件**：
  - `src/tests/test_tooling/file/test_read.py`
  - `src/tests/test_tooling/file/test_write_file.py`
- **重构策略**：
  - 将原有通过 `fake_interact` 返回 `deny` 的用例，调整为传入**无交互的纯门禁上下文**。
  - 断言在未提供 Grant 的情况下，工具直接返回 `Path traversal blocked` 且报错，不产生交互请求。

### 5.2 授权放行与业务错误测试（断言 Grant 生效）
- **涉及文件**：
  - `src/tests/test_tooling/file/test_read.py` (`test_external_nonexistent_path_still_blocked`)
  - `src/tests/test_tooling/permission/test_tool_exec_with_grants.py`
- **重构策略**：
  - 改为在执行工具前先向 `PermissionService` 添加 `runtime` 或 `session` Grant。
  - 验证当文件不存在时，工具在持有 Grant 的情况下**顺利通过门禁**，由文件系统层返回预期的 `File not found`（而不是权限受阻）。

### 5.3 审批交互与提升全流程测试（提升至调度层测试）
- **涉及文件**：
  - `src/tests/test_tooling/permission/test_permission_phase2.py`
  - `src/tests/test_tooling/permission/test_permission_phase3.py`
- **重构策略**：
  - 将原有测试“在工具内部选择 session_file/persistent_file 能否提权”的测试用例，迁移至 `src/tests/test_agent/adapters/langgraph/`，或并入已有的 `src/tests/test_tooling/permission/test_permission_flow_grants.py`（该文件已存在，并非新建）。
  - 通过 `PermissionFlow._authorize_tool_calls` 测试一次性审批并成功注入 Session/Persistent Grant 的全流程。

### 5.4 租约生命周期语义依赖类测试（随改动 3 调整）
- **涉及文件**：
  - `src/tests/test_tooling/permission/test_runtime_grant_lifecycle.py`：专测“单工具租约退出即清理 runtime grants”。执行路径移除单工具嵌套后，`execution_lease_for_tool` 自身语义保留（见 4.3-4），本文件按新语义重校准断言——清理时机由批次租约触发。
  - `src/tests/test_tooling/permission/test_created_path_grants.py`（12 处直接调用 `execution_lease_for_tool`）：逐一核实所包裹场景在移除执行路径单工具嵌套后断言仍成立。
  - `src/tests/test_tooling/permission/test_permission_phase4.py`（同直接使用 `execution_lease_for_tool`）。
- **既有回归保护（保留不改）**：
  - `src/tests/test_agent/adapters/langgraph/runtime/test_execute_tools_guard.py` 中的 `test_execute_approved_batch_keeps_runtime_grants_for_file_lock_waiters` 与 `test_execute_approved_batch_cancels_pending_tasks_on_outer_cancellation` 直接锁定批次租约包裹行为，是改动 3 的目标语义回归保护，应保持绿。

### 5.5 死代码删除连带测试（随改动 2 调整）
- `src/tests/test_tooling/permission/test_permission_phase2.py`：删除 `test_tool_grant_lock_serializes_prompts_for_same_path` 与 `test_tool_grant_lock_defers_final_target_until_user_choice`（被测的 target_locker 提示串行化机制已移除）；`test_context_grants_are_refreshed`、持久化提权两个用例改为直接经 `PermissionService.add_grant` 注入 Grant。
- **修正（实施核实）**：`src/tests/test_tooling/test_make_interact_callback.py` **保留不改**——`_make_interact_callback` 因 `AgentToolRuntime` 仍在使用而保留（见 4.2-5 修正）。

---

## 6. 不变量与禁止修改事项 (Invariants & Forbidden Changes)

1. **不可绕过沙箱边界（Fail-Closed Invariant）**：
   - 移除底层交互后，任何未在调度层获得 Grant 的外部路径请求**必须立刻拒绝**，绝不允许隐式自动放行。
2. **禁止修改对外暴露的 UI Protocol Schema**：
   - `InteractionPermissionTool`、`UiPermissionRequest` 等结构必须保持向前兼容。
3. **禁止默认持久化授权**：
   - 用户确认选项默认值必须依然是单次允许，不得默认持久化。
4. **不可影响子代理隔离性**：
   - 子代理（`subagent`）遵循只读快照机制不变。

---

## 7. 涉及文件清单与职责

| 文件路径 | 模块性质 | 调整职责 |
| :--- | :--- | :--- |
| `src/voidx/tooling/policy/filesystem/grants.py` | 策略层 | 调整 `resolve_access` 判定顺序，使已有 Grant 优先于文件存在性检查 |
| `src/voidx/tooling/application/authorization.py` | 应用层 | 移除 `authorized_path` 内部的交互、锁与写入死代码，转为纯门禁 |
| `src/voidx/tooling/application/permission_service.py` | 应用层 | 调整 `runtime_grants` 释放机制与租约定义 |
| `src/voidx/agent/adapters/langgraph/runtime/tool_executor/executor.py` | 运行时适配 | 移除单工具级别冗余租约，统一由外层批次租约管理声明周期 |
| `src/voidx/agent/adapters/langgraph/runtime/permission_flow.py` | 运行时适配 | 固化调度层为唯一审批源，统一外部路径授权选项 |
| `src/voidx/tooling/domain/grants.py` | 领域模型 | 规范化 `PathGrantChoice` 枚举定义 |
| `src/tests/test_tooling/file/` 及 `permission/` | 测试层 | 按照迁移方案分类重构测试用例 |

---

## 8. 验证计划与测试用例设计

### 8.1 验证命令
```bash
./test.py --backend -- src/tests/test_agent/adapters/langgraph/ src/tests/test_tooling/
```

### 8.2 重点测试矩阵
1. **外部不存在文件单次审批**：Agent 调用 `read` 外部不存在文件，`PermissionFlow` 审批一次后，底层工具直接返回 `File not found`，无二次弹窗。
2. **同批次多个外部文件调用**：同一轮并发/串行调用多个外部文件，调度层审批一次，批次租约维持到整批结束，中间无二次弹窗。
3. **越权外部路径纯门禁拦截**：未经调度层审批的外部路径直接报错 `Path traversal blocked`，无弹窗。
4. **AI 审批通道闭环**：`ai_approval` 模式下 AI 自动批准外部路径调用后，注入的 runtime Grant 能被底层 `authorized_path` 命中放行，无人工弹窗、无误拦（回归 permission_flow.py 中 `_apply_runtime_grant` 的既有接线）。
5. **子代理 Fail-Closed 回归**：子代理未经审批访问外部路径，底层直接返回 `Path traversal blocked` 且不弹窗（子代理底层本就无交互端口，行为应保持与改造前一致；子代理审批缺口见 2.3）。
6. **协议兼容性回归**：`python.py scripts/export_ui_protocol_schema.py` 确保协议向后兼容。
7. **死代码清除完整性检查**：全仓库 grep 确认无残留引用——`authorization.interaction`、`authorization.target_locker`、`authorization.grant_writer`、`authorization.execution_lease_factory`、`CallbackInteractionPort`，以及已删除的 `_grant_choice`、`_release_lock`、`_call_add_grant`、`_approval_precondition` 辅助函数与 `src/voidx/tooling/ports/interaction.py` 模块。
