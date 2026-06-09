# UI 事件系统后续改进：Payload 版本策略与内存优化

> **Status: Pending**

## 来源

从 `docs/archive/ui-events-improvements-design-2026-06-08.md` Phase B 拆出。

## 改进项

### 1. 事件 payload 版本策略评估（P2）

**问题**：`UiEventBase` 没有 payload-level 版本号。当前 frontend protocol envelope 有 `PROTOCOL_VERSION = 1`，通过 envelope 字段 `v` 发送给前端。如果未来支持前端/后端独立部署，单靠 envelope version 可能不足以表达某个事件 payload 的局部兼容变化。

**建议**：

- 优先评估是否提升 envelope `PROTOCOL_VERSION`（成本最低）
- 只有确实需要事件级兼容分支时，再考虑给 `UiEventBase` 增加 payload schema version
- 若引入 payload version，需同步更新 WebSocket gateway 序列化逻辑

**权衡**：当前前端和后端同步更新，且已有 envelope-level version，此项不紧迫。

**工作量**：小（评估为主，若实施则需改 schema + gateway）

### 2. `_tool_nodes` 和 `_agent_nodes` 清理策略（P3）

**问题**：`_tool_nodes` 和 `_agent_nodes` 字典只在 `TurnStarted` 和 `ResetRequested` 时清空。单个 turn 内工具调用非常多时，字典会持续增长。

**约束**：不能在 `ToolFinished` 时直接移除条目——现有事件顺序允许 `ToolFinished` 之后再收到 `ToolResultAppended` / `FileChangeAppended`，这些事件仍需要通过 `tool_call_id` 找到 parent node。

**候选方案**：

- **方案 A**：turn 结束时统一清空（当前行为，已实现）
- **方案 B**：引入 per-turn 上限，超出时只清理已无后续引用的旧条目（需定义"无后续引用"的判定条件）
- **方案 C**：引入 `ToolLifecycleDone` 事件，在确认无后续事件后才清理（需改事件 schema 和 agent 侧发事件逻辑）

**权衡**：当前 turn 内工具数量通常不超过几十个，实际内存影响可忽略。

**工作量**：中

### 3. 队列背压机制（P3）

**问题**：`emit_nowait()` 使用 `put_nowait()`，如果 consumer 处理速度跟不上生产速度，队列会无限增长。

**候选方案**：

- 设置队列上限（如 1000），`emit_nowait()` 在队列满时丢弃最旧的事件或记录 warning
- 流式事件（`AssistantStreamUpdated`）丢弃中间帧可接受（最终会 commit），但 `ToolStarted` 等一次性事件不可丢弃
- 需要区分"可丢弃"和"不可丢弃"事件类别

**权衡**：当前实际使用中未观察到队列积压问题。

**工作量**：大

## Non-goals

- 不改变事件总线的核心投递语义（单消费者、FIFO、有序）
- 不修改 WebSocket gateway 的序列化格式
- 不在 `ToolFinished` 时清理 `_tool_nodes`
- 不实现事件持久化或回放
