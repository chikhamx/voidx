> **Status: Done** — P2 (500 条截断) 和 P3 (skill run state 注入) 已在代码中实现；`count_messages()` 已存在；强制 resume compaction 逻辑已落地。

# Resume 优化设计文档

Date: 2026-06-06

## 1. 当前 Resume 流程

```
用户启动 voidx（resume 模式）
  │
  ├─ VoidXGraph.__init__()
  │    ├─ _compaction_summary = ""
  │    ├─ _task_state = TaskState()
  │    ├─ _task_run = TaskRun()
  │    ├─ _interaction_mode = AUTO
  │    ├─ _session_msg_cache = None
  │    └─ _session_date = session.created_at
  │
  ├─ run()
  │    └─ _restore_runtime_state()
  │         ├─ _interaction_mode
  │         ├─ _task_state
  │         ├─ _task_run
  │         └─ _compaction_summary
  │
  ├─ _show_startup(append_transcript=True)
  │
  └─ 用户输入第一条消息 -> _run_once()
       ├─ count_messages() 预检（resume 首轮）
       ├─ load_messages() / _session_msg_cache
       ├─ messages_from_rows()
       ├─ append 当前用户消息
       ├─ _maybe_compact(force=force_resume_compaction)
       ├─ graph.ainvoke()
       │    └─ _prepare_with_stream()
       │         ├─ _instruction.system()
       │         ├─ _instruction.skill_context_for()
       │         ├─ _merge_skill_runs(_restored_skill_runs(), skill_context.runs)
       │         ├─ RuntimeContextBuilder(...).build()
       │         └─ context.apply_to_messages()
       └─ 持久化新消息 + runtime state
```

### 1.1 已恢复的数据

| 数据 | 来源 | 时机 |
|------|------|------|
| interaction_mode | `session_runtime_state` | `run()` |
| task_state | `session_runtime_state` | `run()` |
| task_run | `session_task_runs` | `run()` |
| compaction_summary | `session_runtime_state` | `run()` |
| 历史消息 | `messages` | `_run_once()` 首次调用 |
| UI transcript | `transcript_nodes` | `_show_startup()` |
| session_date | `sessions.created_at` | `__init__()` |

### 1.2 关键事实

当前 compaction 已经会调用 `delete_messages_through(session_id, last_message_id)` 删除 `messages` 表中被摘要覆盖的 head 消息。因此，已经成功压缩过的 session 在 resume 后执行 `load_messages()` 时，本身只会加载剩余 tail 消息。

这意味着不需要再额外持久化 `last_compaction_message_id` 来实现 tail-only resume。真正的问题集中在：

- 未压缩的长 session 首轮仍会加载并构造全部消息。
- ~~当前 500 条保护逻辑在 compaction 之前截断消息，破坏语义。~~ **已修复**：改为 `count_messages()` 预检 + 强制 compaction。
- ~~runtime 恢复出来的 `task_run.skill_runs` 没有被注入当前 prompt。~~ **已修复**：`_prepare_with_stream()` 通过 `_merge_skill_runs()` 合并恢复的 skill run state。

## 2. 问题

### P1: 未压缩长 session 首轮成本高

如果历史消息从未被 compaction 删除，resume 后第一条用户消息会触发：

1. `load_messages()` 从 DB 加载全部 `MessageRow`
2. `messages_from_rows()` 逐行构造 LangChain 消息对象
3. `_maybe_compact()` 估算 token 并可能执行 compaction
4. `RuntimeContextBuilder.build()` 重建 system prompt 和 task context

对于长 session，这是首轮不可避免的恢复成本。后续轮次可以依赖 `_session_msg_cache` 和已压缩后的 tail。

### P2: 500 条暴力截断会丢语义 ✅ 已修复

~~当前 `_run_once()` 在 `_maybe_compact()` 之前执行：~~

```python
# 旧逻辑（已移除）
if len(session_msgs) > 500:
    session_msgs = session_msgs[-200:]
```

**当前实现**（`turn_mixin.py:70-93`）：使用 `count_messages()` 预检，当消息数 > 500 且无 compaction_summary 时，标记 `force_resume_compaction=True`，调用 `_maybe_compact(force=True, ask=False)`。不再做无语义截断。

### P3: 恢复出的 skill run state 没有进入 prompt ✅ 已修复

~~`_restore_runtime_state()` 会恢复 `task_run.skill_runs`，但 `_prepare_with_stream()` 当前只把本轮重新匹配的 `skill_context.runs` 传给 `RuntimeContextBuilder`。~~

**当前实现**（`core.py:377-381`）：`_prepare_with_stream()` 通过 `_merge_skill_runs()` 合并三类来源：
1. `_restored_skill_runs(self._task_run)` — resume 恢复的
2. `state.get("skill_runs")` — 上一轮传递的
3. `skill_context.runs` — 本轮匹配的

合并结果传入 `RuntimeContextBuilder` 和 prepare 节点返回值。

### P4: MessageRuntimeSnapshot 未被主流程消费

`save_message_runtime_snapshot()` 每轮写入 `message_runtime_snapshots`。目前主 resume 流程不读取它，主要恢复依赖 `session_runtime_state` 和 `session_task_runs`。这属于可清理的写入开销，但不是 resume 正确性的主风险。

### P5: context_frames 主流程未读，但仍有调试价值

`context_frames` 记录 LLM 调用帧和 hash，可用于调试、prompt cache 分析或后续增量构建。当前不建议在本阶段删除。

## 3. 不采用的方案

### 3.1 不新增 `last_compaction_message_id`

理由：

- compaction 已经物理删除被摘要覆盖的 head 消息。
- `load_messages()` 对压缩过的 session 已经是 tail-only。
- 新增字段会制造第二套 compaction 边界状态，带来一致性成本。

如果未来 compaction 改为保留完整消息并只标记隐藏状态，再重新评估该字段。

### 3.2 不做后台预压缩

后台预压缩会引入 UI 状态、用户输入竞态和 compaction agent 失败处理。本阶段先在首轮显式处理：检测到长且未摘要的 session 时，显示状态并强制 compaction，保证语义正确。

## 4. 设计方案

### 4.1 移除 500 条截断，改为长 session 强制 compaction ✅ 已实现

在 `_run_once()` 首次加载 session 消息时执行轻量预检：

1. ~~新增 `count_messages(session_id)`，只做 `COUNT(*)`。~~ **已存在**（`session.py:193`）
2. 当 `_session_msg_cache is None` 且消息数超过阈值，并且没有 `compaction_summary` 时，标记本轮需要 resume compaction。
3. 加载完整消息并构造消息对象。
4. 调用 `_maybe_compact(..., force=True, ask=False)`，让 compaction agent 对旧 head 生成摘要。
5. 如果没有可压缩的完整旧 turn，则继续使用完整消息，不做无语义截断。

阈值沿用原保护逻辑的 500 条，作为第一版保守实现。

### 4.2 继续依赖现有 compaction 持久化

`_persist_compaction()` 继续负责：

- 保存 `compaction_summary`
- 删除 `id <= last_message_id` 的旧消息
- 同步 `_session_msg_cache`

这样 resume 后只需 `load_messages()`，不需要额外 tail loader。

### 4.3 注入恢复出的 skill run state ✅ 已实现

在 `_prepare_with_stream()` 中合并两类 skill run：

- `self._task_run.skill_runs.values()`：resume 恢复出的历史运行状态
- `skill_context.runs`：本轮根据用户输入重新匹配出的 skill

按 skill name 去重，本轮匹配结果覆盖同名恢复状态。合并后的结果传入：

- `RuntimeContextBuilder(skill_runs=merged_skill_runs)`
- prepare 节点返回的 `skill_runs`

这样 prompt 的 `Skill run state` 会包含 resume 恢复状态，后续 `task_run.merge_skill_runs()` 也不会因为 prepare 返回值丢掉它。

### 4.4 暂不清理 MessageRuntimeSnapshot 和 context_frames

本阶段只修 correctness 和 resume 首轮体验。后续可单独做持久化瘦身：

- `MessageRuntimeSnapshot`：评估是否删除主流程写入或改为 debug-only。
- `context_frames`：保留，等待 context incremental build / prompt cache 分析消费。

## 5. 实现计划

| 步骤 | 描述 | 文件 | 状态 |
|------|------|------|------|
| 1 | 新增 `count_messages()` | `src/voidx/memory/session.py` | ✅ 已存在 |
| 2 | `_run_once()` 移除 500 条截断和 truncation notice | `src/voidx/agent/graph/turn_mixin.py` | ✅ 已实现 |
| 3 | `_run_once()` 对长且无 summary 的 resume 首轮强制 compaction | `src/voidx/agent/graph/turn_mixin.py` | ✅ 已实现 |
| 4 | `_prepare_with_stream()` 合并恢复的 skill run state | `src/voidx/agent/graph/core.py` | ✅ 已实现 |
| 5 | 增加 focused tests 覆盖长 session compaction 和 skill state 注入 | `tests/` | 待确认 |

## 6. 风险与权衡

| 风险 | 缓解 |
|------|------|
| 未压缩长 session 首轮仍要加载完整消息 | 这是 compaction agent 生成摘要所需输入；相比截断，优先保证语义正确 |
| 强制 compaction 可能多一次 LLM 调用 | 只在消息数超过阈值且无 summary 时触发 |
| 没有完整旧 turn 可压缩 | `_maybe_compact()` 会跳过；不再伪造 truncation notice |
| 恢复 skill run 和本轮匹配 skill 重名 | 本轮匹配覆盖恢复状态，保证当前用户输入优先 |

## 7. 后续工作

- 与 `docs/specs/2026-06-06-context-incremental-build-design.md` 合并规划消息对象缓存、AGENTS.md mtime cache、SystemMessage 复用。
- 单独评估 `message_runtime_snapshots` 是否改为 debug-only。
- 给 `context_frames` 增加实际消费路径或配置开关。
