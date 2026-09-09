# MCP / LSP 初始化事件化与 TUI 临时状态显示设计规范

> **Status: Done** — Archived on 2026-09-09.

- 日期：2026-09-09
- 状态：Approved（已确认设计，待实施）
- 目标受众：人类开发者与 LLM 实施代理

---

## 1. 目标与背景 (Goal & Background)

### 1.1 现状与痛点
在当前实现中，Agent 启动（`run_loop.py`）时的 MCP 服务器连接以及 LSP 初始化与预热（`warming...` → `ready` / `failed`）直接通过 `self._ui.dock.append_message()` 写入。
导致以下问题：
1. **历史污染**：状态输出被当作永久消息追加至会话 transcript（消息树）中，不仅占据大量终端视口，且在跨 turn 或历史回溯时产生噪声；
2. **多端处理脆弱**：Web/Desktop 前端需要通过文本匹配（`suppressed-runtime-noise`，匹配 `MCP connecting:`、`→`、`warming...`）来在渲染层强行压制这些消息；
3. **缺少结构化状态**：UI 层无法获知各服务的结构化就绪状态（类别、名称、状态、耗时或具体错误信息）。

### 1.2 目标 UX (Target UX)
1. **事件驱动**：MCP 连接与 LSP 检测/预热过程封装为标准 UI 事件（`IntegrationStartupUpdated` 与 `IntegrationStartupFinished`）。
2. **TUI 临时显示**：在终端 TUI 中，类似 `thinking` 流式输出的临时插槽动态展示连接与预热进度，不向 transcript 消息树写入任何永久节点。
3. **短暂展示后刷掉**：当所有服务完成连接/预热后，界面保持终态（`ready` / `failed`）短暂展示（约 0.8 ~ 1.0 秒），让用户感知就绪情况，随后自动清除（刷掉），终端恢复干净的输入提示界面；若用户在此期间提前发起新 turn，立即刷掉以便 prompt 和后续 turn 正常渲染。

---

## 2. 详细设计 (Detailed Design)

### 2.1 事件契约 (Event Schema)

文件路径：`src/voidx/agent/domain/ui_events.py`

#### 2.1.1 状态项模型 `IntegrationStartupItem`
```python
class IntegrationStartupItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: Literal["mcp", "lsp"]
    key: str                                    # 唯一键，如 "mcp:tavily", "lsp:python"
    label: str                                  # 显示名，如 "tavily", "python"
    detail: str = ""                            # 附加信息，如路径/来源
    status: Literal["connecting", "warming", "ready", "failed"]
    error: str = ""                             # 失败时的错误信息
```

#### 2.1.2 更新事件 `IntegrationStartupUpdated`
```python
class IntegrationStartupUpdated(UiEventBase):
    kind: Literal["integration_startup.updated"] = "integration_startup.updated"
    items: list[IntegrationStartupItem]
```

#### 2.1.3 完成事件 `IntegrationStartupFinished`
```python
class IntegrationStartupFinished(UiEventBase):
    kind: Literal["integration_startup.finished"] = "integration_startup.finished"
```

并将两个事件加入 `UiEvent` 联合类型导出，保证系统类型安全与序列化完备。

---

### 2.2 生命周期与编排流程 (Lifecycle & Orchestration)

文件路径：`src/voidx/presentation/terminal/run_loop.py`

```
┌────────────────────────────────────────────────────────────┐
│                  TerminalRunLoop.run()                     │
└──────────────┬───────────────────────────────┬─────────────┘
               │                               │
        (MCP Startup)                    (LSP Startup)
               │                               │
       enabled_mcp_names               initialize_lsp()
               │                               │
      emit(Updated: connecting)       emit(Updated: warming)
               │                               │
       await start_mcp()               await warm_up_lsp()
               │                               │
      emit(Updated: ready/failed)     emit(Updated: ready/failed)
               │                               │
               └───────────────┬───────────────┘
                               │ (全部结束)
                        asyncio.sleep(0.8)
                               │
                   emit(Finished: completed)
```

1. **MCP 阶段**：
   - 获取 `enabled_mcp_names()`，为每个服务器发射 `status="connecting"` 的 `IntegrationStartupItem`。
   - 执行 `await self._integrations.start_mcp()`。
   - 读取 `self._integrations.mcp_statuses()`：成功的标记为 `ready`，异常的标记为 `failed`（附加 `error`）。
   - 发射 `IntegrationStartupUpdated`。

2. **LSP 阶段**：
   - 执行 `await self._integrations.initialize_lsp()`。
   - 对可用的 LSP 服务器发射 `status="warming"` 的 `IntegrationStartupItem`。
   - 执行 `await self._integrations.warm_up_lsp()`。
   - 解析各语言预热结果：`ok` 标记为 `ready`；报错解析 detail 并标记为 `failed`。
   - 发射 `IntegrationStartupUpdated`。

3. **结束与延迟清除**：
   - MCP 与 LSP 均进入终态后，短暂等待 `0.8 ~ 1.0` 秒，发射 `IntegrationStartupFinished()`。
   - 若发生未捕获异常，发射 `failed` 状态并正常触发 `IntegrationStartupFinished()`。

---

### 2.3 Dock 与事件消费 (Dock & Event Consumer)

#### 2.3.1 `BottomInputDock` (`src/voidx/presentation/output/dock/app.py`)
在 `BottomInputDock` 中维护临时状态，无需向 `_tree` 插入消息节点：
- 内部状态：
  - `_integration_startup_items: dict[str, IntegrationStartupItem]`
  - `_integration_startup_active: bool`
- 公共接口：
  - `set_integration_startup_items(items: list[IntegrationStartupItem]) -> None`: 更新内部项字典并标记激活，调用 `self.refresh()`。
  - `clear_integration_startup() -> None`: 清空内部项字典并标记未激活，调用 `self.refresh()`。
  - `has_active_integration_startup() -> bool`: 当且仅当 `_integration_startup_active` 为 True 且字典非空时返回 True。
  - `active_integration_startup_lines(width: int) -> list[str]`:
    格式化输出行（单行宽度按 `width` 截断，使用 ANSI / Rich 样式）：
    - MCP 汇总/逐项行：`[dim]MCP connecting: foo, bar...[/dim]` 或就绪状态。
    - LSP 逐项行：`  [cyan]{lang}[/cyan] [dim]→[/dim] {path}{source} [green]ready[/green]` / `[dim](warming...)[/dim]` / `[red]failed[/red]`。

#### 2.3.2 `DockEventConsumer` (`src/voidx/presentation/output/events/consumers.py`)
- 处理 `IntegrationStartupUpdated`:
  ```python
  case IntegrationStartupUpdated() as e:
      self._dock.set_integration_startup_items(e.items)
  ```
- 处理 `IntegrationStartupFinished`:
  ```python
  case IntegrationStartupFinished():
      self._dock.clear_integration_startup()
  ```
- 处理 `TurnStarted`:
  - 启动新 turn 时调用 `self._dock.clear_integration_startup()`，防止临时状态遮挡思考/输入。

---

### 2.4 TUI 临时区域渲染 (TUI Ephemeral Rendering)

文件路径：`tui/voidx_cli/render_frame.py`

现有逻辑中，`thinking_stream_elements` 使用了 Top Region 的 index 3 插槽（位于输入框紧邻上方）：
```python
def _active_thinking_stream_elements(self, width: int) -> list[Text]:
    lines = dock.active_thinking_stream_lines(width)
    if not lines:
        lines = dock.active_integration_startup_lines(width)
    if not lines:
        return []
    return self._transcript_elements_for_rows(lines, width, len(lines))
```
- **复用效果**：
  - 当 LLM 思考流活跃时，优先展示思考流；
  - 启动阶段无思考流时，无缝复用该动态高度临时插槽；
  - 完全不侵入 `layout.py` 的物理视口裁剪与视口计算逻辑；
  - 刷掉时（`lines` 变为空），视口高度自动回退，完全不留痕迹。

---

## 3. 受影响文件与改动范围 (Affected Files)

| 文件路径 | 职责范围 | 改动性质 |
|---------|---------|---------|
| `src/voidx/agent/domain/ui_events.py` | UI 事件定义 | 新增 `IntegrationStartupItem`, `IntegrationStartupUpdated`, `IntegrationStartupFinished`，更新 `UiEvent` 联合体 |
| `src/voidx/presentation/output/dock/app.py` | Dock 状态与临时行生成 | 维护 `_integration_startup_items`，提供 `active_integration_startup_lines` 等接口 |
| `src/voidx/presentation/output/events/consumers.py` | 事件消费者适配 | 转发 `IntegrationStartupUpdated` 与 `IntegrationStartupFinished` 到 Dock，并在 `TurnStarted` 时清理 |
| `src/voidx/presentation/terminal/run_loop.py` | 启动生命周期编排 | 替换原本直接追加文本的 `append_message`，通过事件总线发射结构化启动状态与延迟完成事件 |
| `tui/voidx_cli/render_frame.py` | TUI 临时插槽绘制 | 在 `_active_thinking_stream_elements` 中回退调用 `active_integration_startup_lines` |

---

## 4. 不变式与禁止项 (Invariants & Forbidden Changes)

1. **禁止持久化到 Transcript**：初始化过程中的任何 intermediate/final 文本严禁调用 `append_message` 或创建 `node_type="message"` 的永久树节点。
2. **保持 Gateway 兼容**：新事件作为标准 `UiEventBase` 派生类，由 `GatewayEventConsumer` 正常广播，前端不会崩溃。
3. **保持 Thinking 流优先级**：若出现思考流（如用户极速输入发起 turn），`thinking` 流必须绝对优先或立即清空启动展示。
4. **不修改 `layout.py` 核心计算**：直接利用现有的 `thinking_stream_elements` 动态区域，不新增第 5 个顶层 region 避免引起未知视口抖动。

---

## 5. 验证与测试方案 (Verification Plan)

### 5.1 自动化测试命令
- **后端事件与 Dock 测试**：
  `./test.py --backend -- src/tests/test_presentation/test_dock_integration_startup.py`（新增针对事件流接收与临时行生成的单元测试）
- **Run loop 启动流程测试**：
  `./test.py --backend -- src/tests/test_presentation/test_run_loop_startup_events.py`（模拟 MCP 与 LSP 启动事件发射）
- **TUI 渲染测试**：
  `./test.py --backend -- tui/tests/test_frame_advanced.py -k "thinking"`（验证临时区域在存在 startup lines 时的正常渲染与清空）
- **整体后端回归**：
  `./test.py --backend`

### 5.2 验收标准 (Acceptance Criteria)
1. 启动 Voidx 时，MCP 和 LSP 的初始化状态在输入框上方临时显示。
2. 当所有服务 ready / failed 后，显示保留约 0.8 秒。
3. 随后临时区域自动消失，终端界面恢复清爽，transcript 历史中无任何 `MCP connecting:` 或 `→ warming...` 历史消息。
4. 所有后端与 TUI 相关测试套件均通过。
