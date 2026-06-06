# Resume 优化设计文档

Date: 2026-06-06

## 1. 当前 Resume 流程分析

### 1.1 完整链路

```
用户启动 voidx（resume 模式）
  │
  ├─ VoidXGraph.__init__()
  │    ├─ _compaction_summary = ""
  │    ├─ _task_state = TaskState()
  │    ├─ _task_run = TaskRun()
  │    ├─ _interaction_mode = AUTO
  │    ├─ _session_msg_cache = None
  │    └─ _session_date = _session_date(session)
  │
  ├─ run()
  │    └─ _restore_runtime_state()          ← 从 DB 恢复 4 个字段
  │         ├─ _interaction_mode ← load_interaction_mode()
  │         ├─ _task_state       ← load_task_state()
  │         ├─ _task_run         ← load_task_run()
  │         └─ _compaction_summary ← load_compaction_summary()
  │
  ├─ _show_startup(append_transcript=True)  ← 恢复 UI transcript
  │
  └─ 用户输入第一条消息 → _run_once()
       ├─ session_msgs = load_messages()    ← 从 DB 加载全部消息
       │    └─ _session_msg_cache = list(session_msgs)
       ├─ msgs = messages_from_rows(session_msgs)  ← 逐行 new *Message
       ├─ msgs.append(turn_msg)             ← 追加当前用户消息
       ├─ _maybe_compact(msgs, ...)         ← 检查溢出并可能压缩
       ├─ graph.ainvoke(initial, ...)
       │    └─ _prepare_with_stream()
       │         ├─ await _instruction.system()        ← 重读 AGENTS.md
       │         ├─ await _instruction.skill_context_for()  ← 重选 skill
       │         ├─ summary = _pending_summary or _compaction_summary
       │         ├─ RuntimeContextBuilder(...).build()  ← 重建所有 sections
       │         └─ context.apply_to_messages(msgs)     ← 替换 SystemMessage + prepend task context
       └─ 持久化新消息 + runtime state
```

### 1.2 恢复的数据

| 数据 | 恢复方式 | 恢复时机 |
|------|---------|---------|
| interaction_mode | `load_interaction_mode()` | `run()` → `_restore_runtime_state()` |
| task_state | `load_task_state()` | 同上 |
| task_run | `load_task_run()` | 同上 |
| compaction_summary | `load_compaction_summary()` | 同上 |
| 历史消息 | `load_messages()` | `_run_once()` 首次调用 |
| UI transcript | `load_transcript()` | `_show_startup()` |
| session_date | `session.created_at` | `__init__()` |

### 1.3 未恢复的数据

| 数据 | 现状 | 影响 |
|------|------|------|
| MessageRuntimeSnapshot | 只写不读 | 每条消息的 intent/goal/phase 快照从未被 resume 使用 |
| context_frames | 只写不读 | 每次 LLM 调用的上下文帧从未被 resume 使用 |
| skill_runs | 通过 task_run 恢复 | ✅ 已覆盖 |
| pending_approval | 通过 task_state/task_run 恢复 | ✅ 已覆盖 |

---

## 2. 问题

### P1: Resume 后首轮上下文重建开销大

Resume 后第一条消息触发全量重建：

1. `load_messages()` 从 DB 加载全部 MessageRow
2. `messages_from_rows()` 逐行构造 LangChain 消息对象（含 `parse_structured_content` JSON 解析）
3. `RuntimeContextBuilder.build()` 重建所有 context sections
4. `context.apply_to_messages()` 替换 SystemMessage + prepend task context

对于 200+ 消息的长 session，这意味着 200+ 次对象构造 + 1 次 system prompt 全量拼接。

### P2: MessageRuntimeSnapshot 从未被消费

每轮 `_run_once` 都调用 `save_message_runtime_snapshot()`，将 intent/goal/phase/available_tool_ids 写入 `message_runtime_snapshots` 表。但 resume 流程中从未调用 `load_message_runtime_snapshot()`。

这些数据占用了 DB 空间和写入开销，但没有任何收益。

### P3: context_frames 从未被消费

每次 LLM 调用都调用 `save_context_frame_from_messages()`，将完整消息序列序列化后写入 `context_frames` 表。但 resume 流程中从未调用 `load_context_frames()`。

同样占用了 DB 空间和写入开销，没有收益。

### P4: Resume 后 compaction 可能立即触发

如果 session 很长，resume 后首轮 `load_messages()` 加载全部消息 → `_maybe_compact()` 检测到溢出 → 立即触发压缩。这导致：

- 用户输入第一条消息后，需要等待压缩完成才能得到响应
- 压缩本身需要一次 LLM 调用（compaction agent）
- 用户体验：输入后长时间无响应

### P5: 大 session 的暴力截断

当 `len(session_msgs) > 500` 时，`_run_once` 只加载最近 200 条消息，并生成 truncation_notice。但这个截断发生在 compaction 之前，意味着：

- 被截断的 300 条消息没有经过 compaction agent 的摘要
- truncation_notice 只是简单说明"有 N 条消息被省略"，不包含任何语义信息
- 与 compaction v2 的"保留完整 tail + LLM summary"设计理念冲突

### P6: _session_msg_cache 在 compaction 后可能不一致

`_persist_compaction()` 调用 `delete_messages_through()` 删除 DB 中的 head 消息，然后更新 `_session_msg_cache`。但如果 compaction 发生在 `_run_once` 内部，而 `_session_msg_cache` 的更新逻辑是：

```python
self._session_msg_cache = [r for r in cache if r.id > last_message_id]
```

这个过滤基于 `last_message_id`，但 `delete_messages_through` 删除的是 `id <= last_message_id` 的消息。如果 cache 中有消息的 id 等于 `last_message_id`，它会被正确过滤掉。但如果 compaction 失败后重试，cache 状态可能不一致。

### P7: Resume 后 skill state 不完整

`_restore_runtime_state()` 恢复了 `task_run`（包含 `skill_runs`），但 `skill_runs` 只记录了 skill 的运行状态（name/phase/turn_count），不包含 skill 的完整上下文（如 brainstorming 的"已提出设计"状态）。

这意味着 resume 后，active skill 的上下文可能丢失，导致 LLM 不知道之前已经完成了哪些 skill 步骤。

---

## 3. 设计方案

### 3.1 核心思路：Resume-Aware Context Assembly

将 resume 流程从"加载全部消息 → 全量重建"改为"按需加载 → 增量组装"：

1. **Resume 时只加载元数据**，不加载全部消息
2. **首轮对话时按需加载消息**，利用 compaction summary 减少需要加载的消息量
3. **复用 compaction summary 作为 resume 的主要上下文来源**
4. **清理未消费的持久化数据**，减少写入开销

### 3.2 方案对比

| 方案 | 描述 | 优点 | 缺点 |
|------|------|------|------|
| A: 轻量 Resume | Resume 时只加载 summary + tail 消息，不加载全部历史 | 启动快，内存占用小 | 需要修改消息加载逻辑 |
| B: 后台预加载 | Resume 时异步预加载消息，用户输入时可能已就绪 | 不阻塞用户输入 | 实现复杂，竞态条件 |
| C: 快照恢复 | 每次 compaction 后保存完整上下文快照，resume 时直接恢复 | 恢复最快 | 快照可能很大，与增量构建冲突 |

**推荐方案 A**：轻量 Resume。实现简单，效果明显，与现有 compaction 机制天然配合。

---

## 4. 详细设计

### 4.1 Resume 时只加载 tail 消息

**现状**：`_run_once()` 调用 `load_messages()` 加载全部消息，然后 `messages_from_rows()` 构造全部 LangChain 消息。

**改进**：新增 `load_tail_messages(session_id, after_id)` 方法，只加载 compaction 后保留的 tail 消息。

```python
# memory/session.py
async def load_tail_messages(session_id: str, after_message_id: int | None = None) -> list[MessageRow]:
    """Load messages after a given ID, or all messages if ID is None."""
    if after_message_id is not None:
        rows = await _fetch_all(
            "SELECT * FROM messages WHERE session_id = ? AND id > ? ORDER BY id",
            (session_id, after_message_id),
        )
    else:
        rows = await _fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    return [MessageRow(**dict(row)) for row in rows]
```

**配合修改**：在 `session_runtime_state` 表中持久化 `last_compaction_message_id`，记录最后一次压缩删除的最大 message id。

```python
# memory/runtime_state.py — 新增字段
async def save_last_compaction_id(session_id: str, message_id: int) -> None:
    await _execute_commit(
        """UPDATE session_runtime_state SET last_compaction_message_id = ? WHERE session_id = ?""",
        (message_id, session_id),
    )

async def load_last_compaction_id(session_id: str) -> int | None:
    row = await _fetch_one(
        "SELECT last_compaction_message_id FROM session_runtime_state WHERE session_id = ?",
        (session_id,),
    )
    if row and row["last_compaction_message_id"] is not None:
        return row["last_compaction_message_id"]
    return None
```

**在 `_persist_compaction` 中保存**：

```python
async def _persist_compaction(self, head_messages):
    # ... 现有逻辑 ...
    last_id = _max_persisted_message_id(head_messages)
    await save_last_compaction_id(self._session.id, last_id)
```

**在 `_run_once` 中使用**：

```python
# run_loop.py — _run_once
if self._session_msg_cache is not None:
    session_msgs = list(self._session_msg_cache)
else:
    compaction_id = await load_last_compaction_id(self._session.id)
    if compaction_id is not None and self._compaction_summary:
        session_msgs = await load_tail_messages(self._session.id, compaction_id)
    else:
        session_msgs = await load_messages(self._session.id)
    if self._session:
        self._session_msg_cache = list(session_msgs)
```

**效果**：对于经过压缩的长 session，resume 后只加载 tail 消息（通常 3-6 个 turn），而不是全部历史。

### 4.2 移除 500 条暴力截断

**现状**：`len(session_msgs) > 500` 时截断到 200 条，生成无语义的 truncation_notice。

**改进**：有了 4.1 的 tail-only 加载，不再需要暴力截断。如果 session 没有经过 compaction 但消息很多，应该先触发 compaction，而不是暴力截断。

```python
# 移除 truncation_notice 逻辑
# if len(session_msgs) > 500: ...  ← 删除

# 改为：如果消息过多且没有 compaction summary，先执行强制 compaction
if len(session_msgs) > 200 and not self._compaction_summary:
    # 首轮强制压缩，避免加载过多消息
    pass  # _maybe_compact 会在后面处理
```

### 4.3 清理未消费的持久化

#### 4.3.1 MessageRuntimeSnapshot

**选项 A**：完全移除 `save_message_runtime_snapshot()` 调用。

**选项 B**：保留写入，但增加读取路径——在 resume 时加载最新一条 snapshot，用于恢复 intent/goal 等状态。

**推荐选项 A**：当前 `task_state` 和 `task_run` 已经通过 `session_runtime_state` 表持久化并恢复，`MessageRuntimeSnapshot` 是冗余的。移除后：

- 减少每轮 1 次 DB 写入
- 减少 `message_runtime_snapshots` 表的体积
- 简化代码

如果未来需要按消息粒度恢复状态，可以重新引入。

#### 4.3.2 context_frames

**选项 A**：完全移除 `save_context_frame_from_messages()` 调用。

**选项 B**：保留写入，增加读取路径——用于 prompt cache 命中率分析、debug 等。

**推荐选项 B（保留但降级）**：context_frames 的设计目的是支持 prompt cache 前缀匹配（`prefix_hash` 字段）。虽然当前未消费，但这是增量构建（compaction-v2 / context-incremental-build）的关键基础设施。建议：

- 保留写入
- 添加 `debug` 标记，非 debug 模式下可跳过
- 未来增量构建方案会消费这些数据

### 4.4 Resume 后的 compaction 优化

**现状**：Resume 后首轮可能立即触发 compaction，用户需要等待。

**改进**：在 `_restore_runtime_state()` 中检查是否需要 compaction，如果需要：

1. 在 `_show_startup()` 之前异步启动 compaction
2. 或者：在 `_run_once()` 中，如果检测到需要 compaction，先显示 "Resuming session..." 状态，再执行压缩

```python
async def _restore_runtime_state(self):
    # ... 现有恢复逻辑 ...
    
    # 预估是否需要 compaction
    if self._session:
        msg_count = await count_messages(self._session.id)
        if msg_count > 100:  # 粗略阈值
            # 标记首轮需要优先检查 compaction
            self._needs_resume_compaction = True
```

### 4.5 Session Date 恢复准确性

**现状**：`_session_date()` 从 `session.created_at` 解析日期。如果 session 跨日，system prompt 中的 `Session Date` 仍然是创建日期。

**改进**：这是设计意图——`Session Date` 代表 session 创建日期，`Current DateTime` 代表当前时间。跨日时 LLM 可以通过两者推断"昨天/今天"的语义。不需要修改。

### 4.6 Skill State 恢复增强

**现状**：`skill_runs` 通过 `task_run` 恢复，但只包含 name/phase/turn_count，不包含 skill 的完整上下文。

**改进**：在 `SkillRunState` 中增加 `context_summary` 字段，记录 skill 的关键状态：

```python
class SkillRunState(BaseModel):
    name: str
    phase: str = "active"  # active | completed | cancelled
    turn_count: int = 0
    context_summary: str = ""  # 新增：skill 的关键状态摘要
```

在 skill 完成或暂停时，由 skill 框架写入 `context_summary`。Resume 后，`active_skill_summaries` 会包含这个摘要，LLM 可以理解 skill 的当前状态。

---

## 5. 实现计划

### Phase 1: 轻量 Resume（核心优化）

| 步骤 | 描述 | 文件 |
|------|------|------|
| 1.1 | 新增 `load_tail_messages()` | `memory/session.py` |
| 1.2 | 新增 `save/load_last_compaction_id()` | `memory/runtime_state.py` |
| 1.3 | DB schema: `session_runtime_state` 增加 `last_compaction_message_id` 列 | `memory/store.py` |
| 1.4 | `_persist_compaction` 中保存 `last_compaction_id` | `agent/graph/compaction.py` |
| 1.5 | `_run_once` 中使用 tail-only 加载 | `agent/graph/run_loop.py` |
| 1.6 | 移除 500 条暴力截断逻辑 | `agent/graph/run_loop.py` |

### Phase 2: 清理冗余持久化

| 步骤 | 描述 | 文件 |
|------|------|------|
| 2.1 | 移除 `save_message_runtime_snapshot()` 调用 | `agent/graph/run_loop.py` |
| 2.2 | 保留 `context_frames` 写入，添加 debug 跳过 | `agent/graph/core.py`, `agent/graph/compaction.py` |

### Phase 3: Skill State 增强

| 步骤 | 描述 | 文件 |
|------|------|------|
| 3.1 | `SkillRunState` 增加 `context_summary` 字段 | `skills/runtime.py` |
| 3.2 | Skill 框架在关键节点写入 `context_summary` | `skills/` 相关文件 |
| 3.3 | `active_skill_summaries` 渲染包含 `context_summary` | `agent/runtime_context.py` |

---

## 6. 风险与权衡

| 风险 | 缓解措施 |
|------|---------|
| `last_compaction_message_id` 与实际 DB 状态不一致 | compaction 是原子操作（delete + save id 在同一事务中），不一致风险低 |
| 移除 MessageRuntimeSnapshot 后无法按消息粒度恢复 | task_state/task_run 已覆盖主要恢复需求；如需可重新引入 |
| tail-only 加载可能遗漏 compaction 前的重要上下文 | compaction summary 已包含关键决策和文件路径；tail 消息保留最近 3-6 个 turn |
| 移除暴力截断后，未压缩的长 session 首轮可能很慢 | 首轮 `_maybe_compact` 会检测溢出并触发压缩；可考虑 resume 时预检 |
