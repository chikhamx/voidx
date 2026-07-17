# AI 审批计数功能设计文档

> **Status: Done** — Archived on 2026-07-17.

## 1. 背景与需求

在启用 `ai_approval` 权限模式时，AI 会自动审查危险的工具调用。为了让用户直观地看到 AI 审批的运行效果，需要引入一个 AI 审批计数器：
- 每当 AI 审批通过（allow）一个工具调用，计数器加 1。
- 该计数需要实时体现在状态栏和权限显示中。
- 需同时支持 Desktop (Web Frontend) 和 TUI 的显示。
- 后端计数逻辑保持一致，且在权限模式切换、会话重置等场景下能正确同步或重置。

## 2. 详细设计

### 2.1 后端状态管理 (`PermissionService`)

- 在 `PermissionService` 中新增 `ai_approval_count` 属性，初始值为 `0`。
- 新增方法 `inc_ai_approval_count(self) -> None`，用于将计数加 1，并递增 `state_revision` 以触发状态更新。
- 在 `permission_mode_label(self) -> str` 中，如果当前模式为 `ai_approval` 且 `ai_approval_count > 0`，则返回 `"AI approval (count)"`，例如 `"AI approval (5)"`。
- 在 `clear_session_permissions(self)` 中重置 `ai_approval_count = 0`。
- 在 `voidx_graph.py` 的 `_apply_settings_update` 中重建 `PermissionService` 时，将旧实例的 `ai_approval_count` 复制到新实例，以保证配置热更新时计数不丢失。

### 2.2 审批触发与计数增加 (`permissions.py`)

- 在 `src/voidx/agent/graph/permissions.py` 中，当 AI 审批结果为 `allow` 时，调用 `self._permission.inc_ai_approval_count()`。
- 计数增加后，如果处于事件模式（`self._ui.via_events()`），则 emit 一个 `RefreshRequested` 事件，以通知 Gateway 广播最新的工作区快照。

### 2.3 协议与状态同步 (`WorkspaceSnapshot`)

- 在 `WorkspaceSnapshot` 协议模型 (`src/voidx/ui/protocol/v2/snapshot.py`) 中新增两个字段：
  - `permission_mode: str = ""`
  - `ai_approval_count: int = 0`
- 在 `GatewaySession._build_workspace_snapshot` 中，从 `runtime_state` 中提取这两个字段并填入 `WorkspaceSnapshot`。
- 在 `run_loop.py` 的 `runtime_state_provider` 中，提供 `permission_mode` 和 `ai_approval_count`。

### 2.4 前端展示 (Desktop / Web UI)

- 在 `frontend/src/services/state.ts` 的 `UiState` 接口中新增 `aiApprovalCount: number` 字段，默认值为 `0`。
- 在 `applyRuntimeState` 中，若 `params` 中包含 `permission_mode` 和 `ai_approval_count`，则同步更新 `uiState.permissionMode` 和 `uiState.aiApprovalCount`。
- 在 `updateStatusBar` 中，当 `uiState.permissionMode` 为 `"ai_approval"` 时：
  - 若 `uiState.aiApprovalCount > 0`，显示为 `"AI 审批 (count)"`，例如 `"AI 审批 (5)"`。
  - 否则显示为 `"AI 审批"`。

### 2.5 TUI 展示

- TUI 的状态栏渲染逻辑在 `tui/voidx_cli/render_status.py` 中，它通过 `self.status.permission_label()` 获取权限文本。
- 由于 `permission_label` 绑定了 `self._permission.permission_mode_label()`，后端返回的 `"AI approval (count)"` 会自动渲染在 TUI 状态栏中，无需修改 TUI 渲染代码。

## 3. 验证计划

- 编写单元测试验证 `PermissionService` 的计数增加、重置以及 label 格式。
- 编写集成测试验证 AI 审批通过时计数正确增加并触发 `RefreshRequested` 事件。
- 运行全量测试确保无回归。
