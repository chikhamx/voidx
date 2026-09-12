# Frontend 与 Backend 功能对齐及健壮性优化规范

> **Status: Done** — Archived on 2026-09-12.

- 日期：2026-09-12
- 状态：设计完成，待实现
- 目标读者：Human + LLM（混合读者，包含决策背景与精确执行规约）
- 适用范围：
  - `frontend/src/rpc/client.ts`（RPC 生命周期与挂起清理）
  - `frontend/src/ui/terminal.ts`、`frontend/src/ui/dock.ts`（终端生命周期与窗口尺寸自适应）
  - `frontend/src/ui/sidebar.ts`、`frontend/src/ui/menus.ts`（会话分叉 `session.fork`）
  - `frontend/src/ui/integrations.ts`（国内搜索引擎 Bocha 配置）
  - `frontend/src/main.ts`（生命周期集成与控制器拆分演进路线）

---

## 1. 背景与目标

在对 `frontend/` 模块与 `src/voidx/presentation/` 后端网关协议与实现进行全面对齐评审后发现，虽然前端整体架构先进且通过了现有的全部 Vitest 测试（987 用例通过），但在**极端网络条件下的健壮性**、**终端进程资源管理**以及**后端现有核心协议能力的 UI 覆盖度**上存在若干优化空间：

1. **RPC 挂起未清理与 UI 假死风险**：当 WebSocket 连接异常中断或工作区重置（`switchWorkspace` / `_setSocket(null)`）时，`pending` 映射表中已发出的请求 Promise 未被 `reject`，导致等待响应的 UI 组件无限期处于 disabled 或 loading 状态。
2. **终端进程孤儿化与显示错位**：前端仅接入了 `terminal.start` 和 `terminal.input`，缺失 `terminal.resize` 与 `terminal.stop`。导致终端面板缩放时 PTY 行列未同步（全屏 CLI 应用如 vim/htop 显示异常），且关闭面板未终结后端 shell 进程。
3. **后端协议能力的 UI 缺口**：
   - 后端已具备 `session.fork` 支持，但前端侧边栏仅有 rename / delete，缺乏从历史轮次分叉探索新会话的入口；
   - 后端已支持国内搜索引擎 Bocha（`bocha.set` / `bocha.delete` / `_bocha_summary`），但前端扩展面板仅集成了 Tavily。
4. **前端入口架构解耦演进**：`frontend/src/main.ts` 单文件过长（2800+ 行），聚合了过多职责，需要明确清晰的控制器（Controller）拆分架构方案。

---

## 2. 详细设计规约

### 2.1 RPC 连接生命周期管理与 Pending 清理

#### 当前行为
- `frontend/src/rpc/client.ts` 中，`rpcCall(method, params)` 将每个请求记录在全局 `pending: Map<number, RpcPending>` 中。
- 仅当收到匹配 `id` 的 response 时才会 `resolve` 或 `reject`。
- 若连接发生 `close`、`error` 或切换 socket 时，`pending` 集合保留不动。等待该 Promise 的业务代码（如配置保存、会话切换）永久阻塞。

#### 目标规范
1. **连接关闭清理机制**：
   - 在 `createWorkerSocket` 的 `close` 与 `error` 事件处理中，或当 `_setSocket(null)` 时，执行 `flushPendingRequests(error)`。
   - 错误类型为 `new Error("RPC connection closed")` 或 `new Error("RPC socket reset")`。
2. **拒绝所有未决请求**：
   - 遍历 `pending`，逐一调用 `entry.reject(error)`。
   - 清空 `pending` 映射表。
3. **断网快速失败 (Fail-Fast)**：
   - `rpcCall` 在进入时若检测到 `!socket || socket.readyState !== WebSocket.OPEN`，立即 `reject(new Error("socket not connected"))`（保持现有逻辑）。

#### 接口与代码契约 (`frontend/src/rpc/client.ts`)
```typescript
export function flushPendingRequests(error: Error = new Error("RPC connection closed")): void {
  for (const [id, entry] of pending.entries()) {
    try {
      entry.reject(error);
    } catch {
      // 忽略单个 reject 异常
    }
  }
  pending.clear();
}
```
在 `_setSocket(ws)` 中：
```typescript
export function _setSocket(ws: RpcSocket | null): void {
  if (socket === ws) return;
  if (!ws && socket) {
    flushPendingRequests(new Error("RPC socket reset"));
  }
  socket = ws;
  // 保持现有通知逻辑...
}
```

---

### 2.2 终端生命周期与窗口自适应 (PTY Resize & Stop)

#### 后端现有协议定义
- `terminal.resize`:
  - 入参：`{ terminal_id: string, cols?: number, rows?: number }`
  - 返回：`{ cols: number, rows: number }`
- `terminal.stop`:
  - 入参：`{ terminal_id: string }`
  - 返回：`{ closed: boolean }`

#### 目标规范
1. **尺寸同步 (`terminal.resize`)**：
   - 在 `frontend/src/ui/terminal.ts` 中，为 `#terminal-pane` 引入 `ResizeObserver`。
   - 根据字符等宽字体度量（或标准 monospace 预估，例如默认宽 8.5px，高 18px）动态计算 `cols = Math.max(20, Math.floor(pane.clientWidth / charWidth))` 和 `rows = Math.max(5, Math.floor(pane.clientHeight / lineHeight))`。
   - 采用 100ms 防抖机制触发 `onTerminalResize(terminalId, cols, rows)`。
   - `main.ts` 监听该回调并向后端发起 `rpcCall("terminal.resize", { terminal_id, cols, rows })`。
2. **生命周期终结 (`terminal.stop`)**：
   - 在 `dock.ts` 关闭终端抽屉、切换会话或切换工作区（`switchWorkspace`）时，调用 `terminateActiveTerminal()`。
   - 触发 `rpcCall("terminal.stop", { terminal_id: activeTerminalId })`。
   - 前端重置终端状态（UI 展示 closed 或清空输出），防止后台无主 bash 进程常驻。

---

### 2.3 会话分叉（Session Fork）能力接入

#### 后端现有协议定义
- `session.fork`:
  - 入参：`{ thread_id: string, title?: string }`
  - 成功返回：`{ thread_id: string, title: string, directory: string, workspace: string, runtime_profile: string, status: "idle" }`
  - 逻辑：后端克隆当前 thread 并在当前 gateway session 中自动注册新线程，返回新 `thread_id`。

#### 前端交互与实现方案
1. **UI 入口**：
   - 位于 `frontend/src/ui/sidebar.ts` 的 `_createSessionActions(threadId)` 中。
   - 在 `rename` 和 `delete` 按钮旁边，增加 `fork` 按钮：
     - 图标：`git-fork` 或 `split` 风格 SVG。
     - 属性：`title="Fork session"`，`aria-label="Fork session"`，`data-action="fork"`。
2. **事件流接入**：
   - `sidebar.ts` 导出 `onThreadFork(callback: (threadId: string) => void)`。
   - `main.ts` 监听 `onThreadFork`：
     ```typescript
     onThreadFork((threadId: string) => {
       rpcCall("session.fork", { thread_id: threadId })
         .then((res: unknown) => {
           const info = res as { thread_id: string };
           if (info?.thread_id) {
             void switchThread(info.thread_id);
           }
         })
         .catch((err: Error) => {
           showSessionError("会话分叉", err);
         });
     });
     ```

---

### 2.4 国内搜索引擎 Bocha 配置接入

#### 后端现有协议定义
- `integrations.get`：返回结构中包含 `"bocha": { "configured": boolean, "source": string, "masked_value"?: string }`。
- `bocha.set`：入参 `{ "api_key": string, "scope"?: "global" | "workspace" }`。
- `bocha.delete`：入参 `{ "scope"?: "global" | "workspace" }`。

#### 前端实现方案
1. **类型扩展 (`frontend/src/ui/integrations.ts`)**：
   ```typescript
   interface BochaConfig {
     configured?: boolean;
     source?: string;
     masked_value?: string;
   }
   export interface IntegrationsSnapshot {
     // ... 现有字段
     tavily?: TavilyConfig;
     bocha?: BochaConfig;
   }
   ```
2. **渲染扩展**：
   - 在 `renderIntegrationsPanel` 的 `Web Search` 分区中，渲染 Bocha 配置行（与 Tavily 对齐）：
     - 显示：配置状态（`configured` / `source` / `masked_value`）。
     - 按钮：`Set Key`（触发 prompt 录入后调用 `bocha.set`）与 `Delete`（二次确认后调用 `bocha.delete`）。
     - 成功后自动调用 `refreshIntegrationsPanel()` 刷新视图。

---

### 2.5 前端主入口架构解耦路线图 (Main Architecture Evolution)

当前 `frontend/src/main.ts` 体积较大，未来演进路线采用分步解耦策略：
1. **第一阶段（协议控制器与状态分离）**：
   - 提取 `src/services/session-controller.ts`：承载 `switchThread`、`openThread`、`removeThread` 以及 `transcript.page` 分页调度。
   - 提取 `src/services/notification-router.ts`：承载 `registerNotificationHandlers` 和 `handleNotification` 的方法路由分派。
2. **第二阶段（快照还原流水线独立）**：
   - 提取 `src/utils/snapshot-installer.ts`：封装 `installBlockedFullSnapshot` 与 `BlockedSnapshotPrebuilt` 的 DOM 置换逻辑。
3. **不变量约束**：
   - 模块解耦必须保证各子模块通过 `_resetForTest()` 导出测试复位钩子。
   - 单向依赖方向：`ui/` -> `services/` -> `rpc/`，严禁循环引用。

---

## 3. 文件修改与影响清单

| 文件路径 | 职责范围 | 预定改动内容 |
| :--- | :--- | :--- |
| `frontend/src/rpc/client.ts` | RPC 网络层核心 | 增加 `flushPendingRequests`，在 socket 关闭或置空时拒绝未决请求并清空 map。 |
| `frontend/src/ui/terminal.ts` | 终端 UI 视图 | 引入 `ResizeObserver` 监听尺寸变化；导出 `onTerminalResize` 与 `onTerminalStop`；增加退出清理机制。 |
| `frontend/src/ui/dock.ts` | Dock 抽屉面板控制器 | 在关闭终端抽屉或切换 Tab 时通知终端进行生命周期维护。 |
| `frontend/src/ui/sidebar.ts` | 侧边栏与会话列表 | 在会话 action 栏增加 Fork 按钮并导出 `onThreadFork` 回调。 |
| `frontend/src/ui/integrations.ts` | 扩展设置面板 | `IntegrationsSnapshot` 增加 `bocha` 字段支持；在 Web Search 区域增加 Bocha 增删配置行。 |
| `frontend/src/main.ts` | UI 组装与编排入口 | 接入 `onThreadFork`、`onTerminalResize`、`onTerminalStop` 的 RPC 联动。 |
| `frontend/test/rpc/client.test.ts` | RPC 单测套件 | 新增断开连接时未决请求被 reject 的测试用例。 |
| `frontend/test/ui/sidebar.test.ts` | 侧边栏单测套件 | 新增 Fork 按钮点击与回调触发测试用例。 |
| `frontend/test/ui/integrations.test.ts` | 扩展面板单测套件 | 新增 Bocha 配置展示、设置与删除测试用例。 |

---

## 4. 约束与禁止项 (Invariants & Forbidden Changes)

1. **不可破坏现有增量流协议**：
   - 增量流（`stream_append_v1`）、全量快照（`workspace.snapshot`）以及阻断安装机制（`BlockedSnapshotPrebuilt`）必须保持严格时序，不得修改 `transcript-dom-window.ts` 中的事务锁机制。
2. **禁止静默忽略异常**：
   - RPC 断开时的 `flushPendingRequests` 必须明确传递包含具体原因的 `Error` 对象，UI 层须通过 Toast 或日志向用户呈现明确提示。
3. **单向依赖规则**：
   - `rpc/` 属于底层基础设施，严禁引用 `ui/` 或 `main.ts`。
4. **环境纯净性**：
   - 模块顶层副作用必须受 `import.meta.env.TEST` 守卫，保证 Vitest 导入时不产生非预期 DOM 操作。

---

## 5. 验收标准与验证方案

### 5.1 验收条件 (Acceptance Criteria)
1. **RPC 健壮性**：
   - 在 WebSocket 意外关闭或断开时，所有尚未收到响应的 `rpcCall` Promise 会在当轮事件循环中被 reject，UI 解除等待状态。
2. **终端资源管理**：
   - 调整浏览器窗口或终端抽屉面板宽度时，防抖触发 `terminal.resize` 并同步至后端；
   - 关闭终端抽屉或切换工作区时，向后端发送 `terminal.stop`，无孤儿进程残留。
3. **会话分叉**：
   - 点击侧边栏任一会话的 Fork 按钮，成功创建新会话并自动切换，新会话保留历史上下文节点。
4. **Bocha 搜索引擎**：
   - 打开 Integrations 弹窗，显示 Bocha 状态；通过 Set Key 录入后，能正确持久化并即时刷新掩码。

### 5.2 验证命令 (Verification Commands)
```bash
# 1. 验证前端全部现有及新增测试
./test.py --frontend

# 2. 聚焦验证 RPC 与 UI 改动用例
./test.py --frontend -- test/rpc/client.test.ts test/ui/sidebar.test.ts test/ui/integrations.test.ts

# 3. 校验后端网关协议测试不受影响
./test.py --backend -- src/tests/test_presentation -v

# 4. 构建验证前端打包产物
cd frontend && npm run build
```
