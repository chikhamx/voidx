# Session 存储架构重构：JSONL Append-Only + SQLite 索引

> **Status: Done**
> Created: 2026-06-09
> Updated: 2026-06-10 — 补充实测数据、修正 context_frame 存储矛盾、新增文件版本历史、修正 JSONL 写入并发模型、修正重放器 summary 处理
> Updated: 2026-06-10 — messages 移出 DB 改用 JSONL、DB 路径改为 ~/.voidx/store/、runtime_state 改为每 session 单行替换
> Updated: 2026-06-12 — 对齐当前代码：补充 message 删除语义、session message_count 索引、现有 rollback 能力、runtime_state/schema 命名差异、subagent 捕获入口
> Updated: 2026-06-13 — 补充删除级联、clear/reset 完整语义、transcript replay 索引和回滚方案
> Updated: 2026-06-14 — 收敛 messages/transcript/log 边界：messages 是模型历史，transcript 只存 UI 结构和引用，文本 transcript log 退出目标存储
> Updated: 2026-06-15 — 标注各模块已实现/剩余状态（对照代码实际实现）
> Updated: 2026-06-15 — 收尾校准：标明 messages/transcript/context/runtime/DB 新主路径已落地，并列出当时剩余 file-history 缺口
> Updated: 2026-06-15 — 补充 `/session del` 首个落地切片：dry-run + 确认删除复用同一候选计算
> Updated: 2026-06-15 — 补齐 `/session list|new|resume` 命名空间别名，顶层旧命令继续兼容
> Updated: 2026-06-15 — 补齐 `/session del` 无参数范围选择器；无 app/dry-run 路径保留默认 30d
> Updated: 2026-06-15 — 补齐 `/session del` 候选展示和空间估算：空间估算只统计 session 文件目录
> Updated: 2026-06-15 — 补齐 `runtime_debug.jsonl` per-message 调试流；`load_message_runtime_snapshot` 只读 JSONL 并应用 runtime 删除 tombstone
> Updated: 2026-06-15 — 补齐 transcript `summary` record 入口和 replay：`summary_offsets` 可 seek 到最新 summary 并跳过被摘要覆盖的旧 turn
> Updated: 2026-06-15 — 补齐 transcript index fallback 重建器：索引缺失/损坏时全量扫描并原子重写 `transcript.idx.json`
> Updated: 2026-06-15 — 补齐 transcript checkpoint snapshot：`transcript.checkpoint.json` 作为重放基线，index 记录 checkpoint offset 并重放后续增量
> Updated: 2026-06-15 — 补齐 subagent JSONL 主路径：生命周期、assistant message、tool result 写入 `subagents/<agent_run_id>.jsonl`
> Updated: 2026-06-15 — 补齐 file-history 主路径：`save_file_version()`、`manifest.jsonl`、短 hash 碰撞规避、write/edit/lsp_format 写前快照；subagent tool context 透传 session_id
> Updated: 2026-06-15 — Phase 4 收敛：新 schema 不再创建旧 SQLite payload 表；messages/transcript/runtime debug 读取只走 JSONL；context frame DB 只保留轻量索引
> Updated: 2026-06-15 — 去除旧根路径 DB 运行时兼容迁移：启动只使用 `~/.voidx/store/voidx.db`，`~/.voidx/voidx.db` 原样忽略

## 0. 实现状态总览

> **【Added 2026-06-15】** 以下基于代码实际对照，标注每个模块的落地进度。`✅ 已实现` = 代码已存在且可工作；`🟡 部分实现` = 有框架但缺关键功能；`❌ 未实现` = 无对应代码。

| 模块 | 状态 | 已落地 | 剩余
|------|------|--------|------
| JSONL 写入器 (`jsonl_store.py`) | ✅ 已实现 | per-session 锁、async IO、append/write/read、offset 追踪、atomic JSON 写入、`drop_session_lock` | —
| Messages JSONL | ✅ 已实现 | `save_message` 只写 `messages.jsonl`，并维护 `sessions.message_count`；`load_messages` 只读 JSONL；`message_deleted`/`session_cleared` tombstone 已落地 | —
| Transcript JSONL | ✅ 已实现 | `replace_transcript` 只写 JSONL，`load_transcript` 只读 JSONL；`transcript_reset`、`summary`、`node`/`node_update`/`turn_start`/`turn_end`；`node_update` 支持覆盖、`body_append`、metadata merge/delete；summary replay 会跳过被摘要覆盖的旧 turn | —
| Transcript index (`transcript.idx.json`) | ✅ 已实现 | index 结构写入、`last_reset_offset` seek、`summary_offsets` seek、`turn_offsets` 记录；索引缺失/损坏时 fallback 全量扫描并重建 index；`transcript.checkpoint.json` 保存 snapshot rows，index 记录 checkpoint offset，replay 从 checkpoint + 后续增量恢复 | —
| Context frames JSONL | ✅ 已实现 | SQLite 只存轻量 frame 索引（file_path 列替代 messages_json）；完整 messages 只写 `context/<id>.jsonl`；`messages_json` 不再保存 payload；`context_frame_deleted` tombstone 已落地 | —
| Delete cascade tombstone | ✅ 已实现 | `message_deleted` + `context_frame_deleted` + `runtime_state_deleted` 同入口写入 | —
| `clear_messages()` JSONL | ✅ 已实现 | 6 步硬重置：清轻量索引/最新态/计数，并写 `session_cleared`/`transcript_reset`/`context_frame_deleted`/`runtime_state_deleted` marker | —
| DB 路径 | ✅ 已实现 | 启动只使用 `~/.voidx/store/voidx.db`；旧根路径 `~/.voidx/voidx.db` 不迁移、不备份、不读取 | —
| `message_count` 索引 | ✅ 已实现 | `sessions.message_count` 列、save 递增、clear 置 0、delete 重算、`list_sessions`/`get_session` 不再 JOIN | —
| Runtime state | ✅ 已实现 | `save_message_runtime_snapshot` 更新 `session_runtime_state` 最新态，并追加 `runtime_debug.jsonl`；不再写 `message_runtime_snapshots` | —
| Subagent JSONL | ✅ 已实现 | `append_subagent_event` 已写入 `subagents/<agent_run_id>.jsonl`；`VoidXGraph._subagent_runner()` 写 `subagent_start`/`subagent_finish`；`run_subagent()` 写 `assistant_message` 和 `tool_result` | —
| File history/rollback | ✅ 已实现 | `SessionChangeTracker` 有当前 turn 内存快照 + `/rollback`；`save_file_version()` 已写 `file-history/<hash>@v<N>` + `manifest.jsonl`；短 hash 碰撞时退到 full hash 文件名；`write`/`edit`/`lsp_format` 写前保存；subagent tool context 已透传 session_id | 后续如需要可新增 `/undo` 恢复命令 |
| `/session` 命令体系 | ✅ 已实现 | `/session list/new/resume` 已作为顶层旧命令别名；`/session del --dry-run [7d|15d|30d|all]` 预览输出；`/session del [7d|15d|30d|all]` 交互确认并删除候选 session；无参数 `/session del` 已支持范围选择器；候选展示包含 updated/workspace/title/msg/bytes，空间估算只包含 session 文件目录 | —
| Runtime debug JSONL | ✅ 已实现 | `runtime_debug.jsonl` per-message 历史流已写入；`load_message_runtime_snapshot` 扫描 JSONL，并应用 `runtime_state_deleted` tombstone；不再读写 `message_runtime_snapshots` | —

## 1. 问题

当前 voidx 所有 session 数据存入单一 SQLite DB（`~/.voidx/voidx.db`），存在以下问题：

### 1.1 DB 膨胀

实测数据（2026-06-10，60 个 session）：

| 数据源 | 大小 | 占 DB 比例 |
|--------|------|-----------|
| `context_frames.messages_json` | 53 MB | 61% |
| `messages.content` | 7 MB | 8% |
| `transcript_nodes` 文本 | 1 MB | 1% |
| 其他（索引、元数据、WAL） | 26 MB | 30% |
| **DB 总计** | **87 MB** | 100% |

清理前极端情况：586 个 session，DB 达 906 MB，其中 `context_frames.messages_json` 占 96%。

**根因**：`context_frames.messages_json` 将全量 LLM 消息序列化后存入 SQLite。单 session 可产生数百帧，每帧 ~0.5 MB。这是 DB 膨胀的首要元凶。

`transcript_nodes` 每次 `_persist_transcript_snapshot` 执行 **DELETE 全部 + INSERT 全量**，无增量能力，写入放大严重。但当前数据量仅 1 MB，属于架构缺陷而非紧迫问题。

### 1.2 无清理策略

Session 无限期累积。清理前 586 个 session 中 115 个空 session、169 个测试 session。只能手动操作 DB。

### 1.3 Subagent transcript 混杂

Subagent 的 UI 节点混在主 session 的 `transcript_nodes` 里，靠 `agent_run_id` 字段区分。无法独立存储、独立重放、独立清理。

### 1.4 命令扁平

`/list`、`/resume`、`/clear` 散落在顶层命令空间，无 `/session` 命名空间，无法扩展 `del`、`new` 等子命令。

### 1.5 无文件版本历史

对比 Claude Code 的 `file-history/` 目录（按 session 存储每个被修改文件的版本快照 `{hash}@v{N}`），voidx 当前既有 `file_state.py` 的 mtime 检查，也有 `ui/session.py` 的 `SessionChangeTracker` 在当前 turn 内为 `write` / `edit` / `lsp_format` 捕获内存快照，并通过 `/rollback` 恢复。

缺口在于：当前 rollback 快照不落盘，生命周期只覆盖当前 turn。用户无法在 resume 后撤回旧修改，也无法查看某次历史 tool call 改了什么。

## 2. 目标

1. **JSONL append-only** 存储 messages、transcript 和 context frames，SQLite 只存轻量索引、runtime state 和全局配置
2. **可配置清理策略**，通过 `/session del` 交互式删除过期 session
3. **Transcript 重放**：从 JSONL 逐行读取重建 OutputTree
4. **Subagent parent-child chain**：独立 JSONL 文件 + subpath 引用
5. **`/session` 命令体系**：`list` / `new` / `resume` / `del`
6. **文件版本历史**：每次 write/edit/lsp_format 前保存原始内容快照，支持后续跨 session undo

### 2.1 设计主线

本重构不是简单“把 SQLite 换成 JSONL”，而是把大 payload、可重放事件流和轻量索引拆开。目标状态按数据流划分：

| 数据流 | 写入位置 | SQLite 保留 | 关键约束 |
|--------|----------|-------------|----------|
| Session 索引与全局配置 | `~/.voidx/store/voidx.db` | `sessions`、`model_profiles`、轻量计数和索引 | list/resume 不再依赖 `messages` 表 |
| LLM messages | `sessions/<sid>/messages.jsonl` | `sessions.message_count` | `message_deleted` / `session_cleared` 必须完整表达删除和清空语义 |
| UI transcript | `sessions/<sid>/transcript.jsonl` + `transcript.idx.json` | 无大 payload | 只存 UI 结构、状态和 message/tool 引用；大 session replay 必须优先 index/checkpoint seek |
| Context frames | `context/<frame-id>.jsonl` + SQLite frame 索引 | `context_frames` 瘦身索引 | 删除必须跟随 message 范围级联，不能留下过期 frame |
| Runtime/debug state | `session_runtime_state` + `runtime.jsonl`，可选 `runtime_debug.jsonl` | 最新 runtime state | resume 只依赖最新 runtime，per-message 历史只用于 debug |
| Subagent 与文件历史 | `subagents/*.jsonl`、`file-history/` | 无 | 独立生命周期，随 session 删除一起清理 |

### 2.2 不变量

后续实现和迁移必须满足这些不变量：

1. Phase 4 后，SQLite 不再保存 session 级大 payload：`messages.content`、`transcript_nodes` 文本、`context_frames.messages_json`、per-message runtime snapshot 都应退出主 DB。
2. 每条 `message` JSONL record 都必须显式带 `content_format`；JSONL 不能依赖 SQLite migration default。
3. `delete_messages_from()`、`delete_messages_through()`、`clear_messages()` 的 JSONL replay 结果必须与当前硬重置/范围删除语义等价，包括 context frame 和 runtime snapshot 级联。
4. `list_sessions()` / `get_session()` 在移除 `messages` 表前必须切到 `sessions.message_count`，并提供从 JSONL 幂等重算计数的修复路径。
5. Transcript replay 对大 session 必须使用 `transcript.idx.json` / checkpoint；索引缺失或损坏时才允许二遍扫描并重建索引。
6. 运行时不做旧 DB 兼容迁移：只初始化/打开 `~/.voidx/store/voidx.db`，旧根路径 `~/.voidx/voidx.db` 原样忽略，历史数据导出如需要走离线工具而不是启动路径。
7. `/session del` 必须先 dry-run 产生候选集合和预估影响；交互确认和实际删除复用同一候选计算。
8. `transcript.jsonl` 不能成为第二份 message history：可引用 `message_id` / `tool_call_id`，但不复制完整 user/assistant/tool content。
9. 文本 `transcript.log` 和普通 logging 只用于 debug/观测，不参与 session resume、replay、迁移或清理语义。

## 3. 设计

### 3.0 当前代码对齐要点

本节是 2026-06-12 根据当前代码补充的实现约束，避免实现时只按旧设计文字推进：

- `store.py` 当前已改用 `DATA_DIR / "store" / "voidx.db"`；启动路径不再迁移或读取旧 `DATA_DIR / "voidx.db"`。新 schema 不再创建 `messages`、`turns`、`transcript_nodes`、`message_runtime_snapshots`。
- `sessions.message_count` 已落地；`get_session()` / `list_sessions()` / `latest_session_for_workspace()` 已读取该轻量索引，不再 JOIN `messages`。
- `context_frames` 当前字段名是 `agent_persona`，不是 `agent_role`。主 agent、compaction、worker subagent 都通过 `agent_persona` 区分来源。
- 当前没有 `session_task_runs` 表。workflow runs 已经存在于 `session_runtime_state.workflow_runs_json`。
- `session_runtime_state` 已经是 per-session UPSERT，并保存 `interaction_mode`、`current_intent`、`previous_intent`、`current_goal_json`、`pending_approval_json`、`workflow_runs_json`、`recent_user_texts_json`、`todo_state_json`、`compaction_summary`、`session_time`。
- `message_runtime_snapshots` 已停止写入；per-message 调试历史只写 `runtime_debug.jsonl`，resume 最新态写 `session_runtime_state`。
- `tree_to_transcript_rows()` 会跳过 startup 和空 separator，按 `turn` 节点递增 `turn_id`，每 turn 内 `node_id` 从 0 重置，并把 `header_style`、`agent_name`、`step_info`、`meta`、`payload` 放进 `metadata`。
- `delete_messages_from()` 用于 cancel 回滚，`delete_messages_through()` 用于 compaction 删除旧上下文。JSONL 方案必须显式支持范围删除或 tombstone，不能只 append message。

### 3.1 存储分层

```
~/.voidx/
├── store/
│   └── voidx.db                          # SQLite：全局配置 + 索引元数据（< 1 MB）
├── sessions/
│   └── <session-id>/
│       ├── messages.jsonl                # 对话消息（user/assistant/tool）
│       ├── transcript.jsonl              # 主对话 transcript
│       ├── transcript.idx.json           # transcript replay/checkpoint 索引
│       ├── runtime.jsonl                 # runtime 删除 tombstone
│       ├── context/
│       │   ├── <frame-id>.jsonl          # 上下文缓存帧（独立文件，不进 transcript）
│       │   └── deletes.jsonl             # context frame 删除 tombstone
│       ├── subagents/
│       │   └── <agent-run-id>.jsonl      # subagent transcript
│       └── file-history/
│           └── <content-hash>@v<N>       # 文件修改版本快照
└── settings.json                        # 全局用户配置
```

**原则**：

- **SQLite 只存轻量数据**：model_profiles、session 索引和计数、context frame 索引、runtime state（每 session 单行）
- **所有大体积 session payload 走 JSONL/文件**：messages、transcript、context frame messages、file-history
- **context frame 和 transcript 是独立数据流**，不混进同一个 JSONL 文件
- **messages 和 transcript 不互相复制**：`messages.jsonl` 是模型上下文 source of truth；`transcript.jsonl` 是 UI OutputTree source of truth，只保存结构、状态、引用和无法从 message 恢复的展示信息
- **文本 transcript log 退出目标存储**：现有 `transcript.log` 若继续存在，只能作为 legacy/debug 日志，不能作为 replay/resume/迁移来源；普通 Python logging 也只做观测和排障，按日志策略 rotate
- **DB 路径在 `~/.voidx/store/` 下**，与 session 文件目录分离

### 3.2 JSONL 数据流格式

每行一个 JSON 对象。参考 Claude Code 的 record type 体系，但适配 voidx 的 OutputTree 模型。

Messages 和 transcript 必须是两个独立数据流：

- `messages.jsonl` 保存 LLM 对话历史，是继续对话、compaction、工具结果回传给模型的唯一权威来源。
- `transcript.jsonl` 保存 UI OutputTree 事件，是恢复界面结构和展示状态的唯一权威来源。它可以保存 `message_id`、`tool_call_id`、`agent_run_id`、短 preview、状态、耗时、折叠/排序/父子关系等 UI 信息，但不能保存一份完整可喂给模型的 message history。

因此不要把 `message` record 混进 `transcript.jsonl`，也不要把 UI-only 字段混进 `messages.jsonl`。

#### Record 类型

| type | 文件 | 用途 | 必选字段 | 替代现有 |
|------|------|------|----------|----------|
| `message` | `messages.jsonl` | 对话消息 | `id`, `role`, `content`, `content_format`, `tool_calls?`, `tool_call_id?` | `messages` 表 INSERT |
| `message_deleted` | `messages.jsonl` | 删除消息范围（cancel/compaction） | `mode`, `first_message_id?`, `last_message_id?`, `reason` | `delete_messages_from` / `delete_messages_through` |
| `session_cleared` | `messages.jsonl` | 清空当前 session 消息和运行时状态 | `reason`, `cleared_at`, `previous_message_count` | `clear_messages` |
| `turn_start` | `transcript.jsonl` | turn 开始 | `turn_id`, `timestamp`, `user_message_id?`, `user_preview?` | 无 SQLite payload 表 |
| `turn_end` | `transcript.jsonl` | turn 结束 | `turn_id`, `timestamp` | 无 SQLite payload 表 |
| `node` | `transcript.jsonl` | 新增 UI 节点 | `turn_id`, `node_id`, `node_type`, `header`, `message_id?`, `tool_call_id?` | 无 SQLite payload 表 |
| `node_update` | `transcript.jsonl` | 节点增量更新 | `turn_id`, `node_id` + 变更字段 | 无 SQLite payload 表 |
| `summary` | `transcript.jsonl` | compaction 摘要 | `turn_id`, `content` | `compaction_summary` 字段 |
| `transcript_reset` | `transcript.jsonl` | 清空或替换 UI transcript | `reason`, `created_at` | `clear_messages` / `replace_transcript` |
| `context_frame_deleted` | `context/deletes.jsonl` | 删除 context frame 范围 | `mode`, `first_user_message_id?`, `last_user_message_id?`, `reason` | context frame 级联删除 |
| `runtime_state_deleted` | `runtime.jsonl` | 删除 session/runtime snapshot 范围 | `mode`, `message_id?`, `first_message_id?`, `last_message_id?`, `reason` | runtime state 级联删除 |

> **【Updated 2026-06-10】** 移除了 `context_frame` record type。Context frame 是 LLM 调用级别的快照（每次调用存一条），和 transcript（UI 节点流）是完全不同的数据流。混进 transcript.jsonl 会导致：重放 transcript 时需要跳过大量 context_frame 记录；context frame 的生命周期和 transcript 不同（compaction 后旧 frame 可删，transcript 要保留 summary）。Context frame 只走 `context/<frame-id>.jsonl` 独立文件。

#### 示例

`messages.jsonl`：

```jsonl
{"type":"message","id":1,"role":"user","content":"修复TODO固定框重复渲染","content_format":"text","created_at":"2026-06-09T07:23:50Z"}
{"type":"message","id":2,"role":"assistant","content":"我来检查一下...","tool_calls":[{"id":"tc_1","name":"read","args":{"file_path":"src/todo.py"}}],"content_format":"text","created_at":"2026-06-09T07:23:51Z"}
{"type":"message","id":3,"role":"tool","content":"1\tclass TodoPanel...","tool_call_id":"tc_1","content_format":"text","created_at":"2026-06-09T07:23:52Z"}
{"type":"message_deleted","mode":"through","last_message_id":2,"reason":"compaction","created_at":"2026-06-09T08:00:00Z"}
```

`content_format` 必须写入每条 message record，默认值为 `"text"`。当前 `MessageRow.content_format` 有 SQLite migration 默认值，但 JSONL 没有 schema default；省略该字段会让 structured attachment / DeepSeek thinking block 的恢复路径变得不确定。

`transcript.jsonl`：

```jsonl
{"type":"turn_start","turn_id":0,"timestamp":"2026-06-09T07:23:50Z","user_message_id":1,"user_preview":"修复TODO固定框重复渲染"}
{"type":"node","turn_id":0,"node_id":0,"parent_node_id":null,"sort_order":0,"node_type":"assistant","header":"assistant","message_id":2,"status":"running","metadata":{"tree_id":"a1b2","payload":{}}}
{"type":"node","turn_id":0,"node_id":1,"parent_node_id":0,"sort_order":1,"node_type":"tool_call","header":"Read file","tool_call_id":"tc_1","status":"done"}
{"type":"node_update","turn_id":0,"node_id":0,"status":"done","elapsed":1.2}
{"type":"turn_end","turn_id":0,"timestamp":"2026-06-09T07:26:08Z"}
{"type":"summary","turn_id":0,"content":"修复了 TodoUpdated 双写问题..."}
```

`user_preview` 和类似 preview 字段只用于列表/首屏展示，可截断；完整 user/assistant/tool content 必须回到 `messages.jsonl` 通过 `message_id` 或 `tool_call_id` 查。纯 UI 节点、summary、错误提示等没有对应 message 的内容，可以直接存入 transcript。

#### 设计决策

- **`node` vs `node_update`**：新增节点用 `node`（全字段），状态变更用 `node_update`（只写变更字段）。重放时先建节点再 patch，避免全量替换。
- **`node_update` 合并语义**：同一 `(turn_id, node_id)` 的多个 update 按文件顺序应用，后写字段覆盖先写字段；`body_lines` 默认替换全量，追加内容必须用 `body_append` 明确表达；`metadata` 默认 shallow merge，清空字段用 `null` 或 `metadata_delete` 列表明确表达。
- **`summary`**：compaction 产生的摘要。重放时遇到 `summary`，跳过该 turn 之前的 node 记录，只保留 summary 内容。这是懒加载的基础。
- **字段映射**：`node` 的字段与现有 `TranscriptNodeRow` 一一对应，迁移成本低。`metadata` 必须保留当前 `tree_to_transcript_rows()` 写入的 `tree_id`、`header_style`、`agent_name`、`step_info`、`meta`、`payload`。
- **message 引用**：message-backed UI 节点必须优先写 `message_id` / `tool_call_id`，不把完整 `content`、`tool_calls`、`tool_result` 复制进 transcript；replay 时按需 join `messages.jsonl` 补齐展示文本。
- **消息删除**：`messages.jsonl` 是 append-only，但当前代码有 `delete_messages_from()` 和 `delete_messages_through()`。读取 messages 时必须先读全量 record，再应用 `message_deleted` tombstone；同时更新 SQLite 中的 `sessions.message_count`。
- **删除字段命名**：record 字段使用当前代码名 `first_message_id` / `last_message_id` / `user_message_id`，不要再引入 `first_id` / `through_id` 这类不对应代码的别名。

#### 删除级联语义

当前删除语义不是只删 `messages.jsonl`：

- `delete_messages_from(session_id, first_message_id)` 追加 message/context/runtime 删除 tombstone，删除匹配的 context frame 轻量索引，并按 JSONL loader 重算 `message_count`
- `delete_messages_through(session_id, last_message_id)` 追加 message/context/runtime 删除 tombstone，删除匹配的 context frame 轻量索引，并按 JSONL loader 重算 `message_count`
- `clear_messages(session_id)` 追加 clear/reset tombstone，删除 context frame 轻量索引和 session runtime 最新态，并将 `message_count` 置 0

JSONL 方案必须为这些级联删除留下可重放记录，而不是只写 message tombstone：

```jsonl
{"type":"message_deleted","mode":"from","first_message_id":42,"reason":"cancel","created_at":"..."}
{"type":"context_frame_deleted","mode":"from","first_user_message_id":42,"reason":"cancel","created_at":"..."}
{"type":"runtime_state_deleted","mode":"from","first_message_id":42,"reason":"cancel","created_at":"..."}
```

`mode` 取值：

- `from`: 删除 `id >= first_message_id`
- `through`: 删除 `id <= last_message_id`
- `all`: 删除该 session 全部记录

范围删除读取规则：

1. `messages.jsonl` loader 应用 `message_deleted`
2. `load_context_frames()` 读取 SQLite 索引后应用 `context_frame_deleted`，再读取剩余 `file_path`
3. runtime state loader 应用 `runtime_state_deleted`；`mode=all` 返回默认 runtime state
4. `sessions.message_count` 可从 JSONL 重算，并作为索引修复来源

### 3.2.1 `clear_messages()` 硬重置语义

当前 `clear_messages(session_id)` 是 6 步硬重置，不只是 “`session_cleared` + message_count=0”：

1. 删除 `context_frames` 轻量索引
2. 删除 `session_runtime_state`
3. 更新 `sessions.message_count = 0`
4. 写 `messages.jsonl` 的 `session_cleared`
5. 写 context/runtime delete tombstone
6. 写 `transcript_reset`

JSONL 对应操作：

- `messages.jsonl`: append `{"type":"session_cleared","reason":"clear_messages",...}`，loader 忽略该行之前所有 message 和 message delete record
- `transcript.jsonl`: append `{"type":"transcript_reset","reason":"clear_messages",...}`，replay 从最后一个 reset 后开始
- `context/deletes.jsonl`: append `{"type":"context_frame_deleted","mode":"all","reason":"clear_messages",...}`
- `runtime.jsonl`: append `{"type":"runtime_state_deleted","mode":"all","reason":"clear_messages",...}`
- SQLite: `sessions.message_count = 0`，删除/失效 context frame 索引行，删除 `session_runtime_state`
- Files: 可保留旧 JSONL 作为 append-only history，也可在 compaction/maintenance 中 rewrite；读取语义必须以 reset marker 为准

`/clear` 新建 session 与 `clear_messages()` 不同：`/clear` 当前会 detach/创建新 session；它不应向旧 session 追加 `session_cleared`，除非明确复用原 session id。

### 3.3 SQLite 保留的职责

> **【Updated 2026-06-12】** SQLite 只存轻量索引、runtime state 和全局配置，不再存大体积 session payload。Messages、transcript、context frame messages 走 JSONL 文件。DB 路径从 `~/.voidx/voidx.db` 改为 `~/.voidx/store/voidx.db`。

| 表 | 保留 | 变更 |
|---|---|---|
| `sessions` | ✅ | 新增/维护 `message_count`，用于替代当前 JOIN `messages` 的 list/resume 计数 |
| `messages` | ❌ 废弃 | 数据迁移到 `messages.jsonl`，Phase 4 删除表 |
| `turns` | ❌ 废弃 | 新 schema 不再创建，数据走 transcript.jsonl 的 `turn_start`/`turn_end` record |
| `transcript_nodes` | ❌ 废弃 | 新 schema 不再创建，数据走 transcript.jsonl |
| `context_frames` | ✅ 瘦身 | 移除 `messages_json`，新增 `file_path` 指向 JSONL |
| `session_runtime_state` | ✅ | 已是 UPSERT（每 session 单行），不变 |
| `message_runtime_snapshots` | ❌ 废弃 | 新 schema 不再创建；最新态写 `session_runtime_state`，debug 历史写 `runtime_debug.jsonl` |
| `model_profiles` | ✅ | 不变（全局配置） |

#### message_runtime_snapshots 合并到 session_runtime_state

> **【实现状态 2026-06-15】** ✅ 已实现。`save_message_runtime_snapshot` 写 `session_runtime_state` 最新 resume 状态，并追加 `runtime_debug.jsonl` per-message 历史流；不再写 `message_runtime_snapshots`。`load_message_runtime_snapshot` 只扫描 `runtime_debug.jsonl`，并应用 `runtime_state_deleted` tombstone，避免 clear/delete 后复活旧快照。

当前 `message_runtime_snapshots` 按 `message_id` 存储每条消息的运行时快照，会无限累积。实际 resume 时只需要最新一份。当前 `session_runtime_state` 已有承载最新 runtime state 的列：

```sql
interaction_mode TEXT NOT NULL DEFAULT 'auto',
current_intent TEXT NOT NULL DEFAULT 'coding',
previous_intent TEXT,
current_goal_json TEXT,
pending_approval_json TEXT NOT NULL DEFAULT '',
workflow_runs_json TEXT NOT NULL DEFAULT '{}',
recent_user_texts_json TEXT NOT NULL DEFAULT '[]',
todo_state_json TEXT NOT NULL DEFAULT '',
compaction_summary TEXT NOT NULL DEFAULT '',
session_time TEXT NOT NULL
```

因此合并时不新增 `intent_confidence` / `intent_source` / `intent_refined` / `available_tool_ids_json`。这些字段来自旧 intent refinement 设计，当前代码已无对应 schema。

合并方案不能只把 `save_message_runtime_snapshot()` 改成覆盖最新 state，因为当前测试和调试路径仍可能按 `message_id` 调 `load_message_runtime_snapshot()`。迁移分两层：

- Runtime 恢复主路径：`save_message_runtime_snapshot()` 同步更新同 session 的 `session_runtime_state`，作为 resume 唯一来源
- Debug 路径：`runtime_debug.jsonl` 允许按 `message_id` 查询历史快照；`message_runtime_snapshots` 已停止写入

`load_message_runtime_snapshot(message_id)` 只查 `runtime_debug.jsonl`，找不到就返回 `None`。不要把 per-message 历史悄悄映射为最新 session runtime state，否则会制造错误的调试结果。

#### DB 路径变更

```python
# store.py
DATA_DIR = Path.home() / ".voidx"
STORE_DIR = DATA_DIR / "store"  # 新增
# DB 路径: STORE_DIR / "voidx.db"
```

启动路径只创建并打开 `~/.voidx/store/voidx.db`。若旧 `~/.voidx/voidx.db` 存在，不迁移、不备份、不读取；若新旧 DB 同时存在，以新 DB 为准。旧数据导出或备份恢复属于离线维护动作，不能进入运行时读取链路。

#### context_frames 瘦身后的 schema

```sql
CREATE TABLE context_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_message_id INTEGER,
    frame_kind TEXT NOT NULL DEFAULT 'main',
    agent_persona TEXT NOT NULL DEFAULT 'voidx',
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prefix_hash TEXT NOT NULL,
    frame_hash TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    file_path TEXT NOT NULL,       -- 新增：指向 context/<frame-id>.jsonl
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

`messages_json` 列移除。加载 context frame 时，先从 SQLite 查索引（prefix_hash、frame_hash），再从 `file_path` 指向的 JSONL 读取消息内容。

当前代码已经使用 `agent_persona`：主路径默认 `voidx`，compaction 使用 `compaction`，subagent worker 使用对应 persona。迁移时不要引入 `agent_role` 新名，避免和现有测试、`ContextFrameRecord`、调用点不一致。

#### messages.jsonl 格式

每行一条消息，与原 `messages` 表字段一一对应：

```jsonl
{"type":"message","id":1,"role":"user","content":"修复TODO","content_format":"text","created_at":"2026-06-09T07:23:50Z"}
{"type":"message","id":2,"role":"assistant","content":"我来检查...","content_format":"text","tool_calls":[{"id":"tc_1","name":"read","args":{"file_path":"src/todo.py"}}],"created_at":"2026-06-09T07:23:51Z"}
{"type":"message","id":3,"role":"tool","content":"1\tclass TodoPanel...","content_format":"text","tool_call_id":"tc_1","created_at":"2026-06-09T07:23:52Z"}
```

加载消息时直接逐行读取 JSONL，无需 SQLite 查询。`id` 字段保留用于 compaction 时的 `delete_messages_through` 引用。

#### Message count 索引

`SessionInfo.message_count` 已是轻量索引，不再从 `messages` 表 JOIN 或 COUNT：

```sql
ALTER TABLE sessions ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0;
```

维护规则：

- `save_message()` 写 `messages.jsonl` 时递增 `sessions.message_count`
- `clear_messages()` 置 0，并追加/写入 `session_cleared` / `transcript_reset` 等 reset marker
- `delete_messages_from()` / `delete_messages_through()` 追加 `message_deleted`，并按 loader 或已知范围重新计算/更新 `message_count`
- `list_sessions()` / `get_session()` 改为读取 `sessions.message_count`，不再 JOIN `messages`

### 3.4 Transcript 重放

#### 现有流程

```
load_transcript(session_id)           # SQLite SELECT
  → TranscriptNodeRow[]
  → transcript_rows_to_tree(rows)     # 构建 OutputTree
  → dock.restore_tree(tree)
```

#### 新流程

```
replay_transcript_jsonl(session_id)   # 逐行读取 JSONL
  → 构建 OutputTree
  → dock.restore_tree(tree)
```

#### 重放器实现

> **【Updated 2026-06-10】** 修正 summary 处理逻辑。原设计在流式重放中遇到 summary 时回溯删除已添加节点，这在树结构上很复杂且容易出错。改为两遍重放：第一遍扫描 summary 位置，第二遍只重放 summary 之后的 node。

> **【Updated 2026-06-13】** 两遍扫描只能作为 fallback。大 session 必须有 seek/index 机制，否则每次 resume 都 O(file size)。新增 `transcript.idx.json`，写入 transcript 时同步维护 summary/reset/checkpoint 偏移。

#### Transcript index

> **【实现状态 2026-06-15】** ✅ 已实现。index 结构写入、`last_reset_offset` seek、`summary_offsets`/`turn_offsets` 记录已实现。`append_transcript_summary()` 会追加 `summary` record 并更新 `summary_offsets`；`load_transcript()` 在 index 有效时优先从 checkpoint、最新 summary offset 或最新 reset offset seek，并将 summary 之前被覆盖的旧 turn 压成一个 `summary` 节点。索引缺失/损坏时会 fallback 全量扫描并原子重写 `transcript.idx.json`。`transcript.checkpoint.json` 保存 snapshot rows 和 offset，checkpoint 之后的 `node_update` 等增量仍会被重放。

`transcript.idx.json` 存轻量 replay 索引：

```json
{
  "version": 1,
  "transcript_size": 1234567,
  "last_reset_offset": 120,
  "turn_offsets": {"0": 121, "1": 9320},
  "summary_offsets": {"0": 53120},
  "last_checkpoint_offset": 980000,
  "last_checkpoint_path": "transcript.checkpoint.json"
}
```

规则：

- `append_transcript()` 每次写入后记录新行起始 byte offset
- `summary` 更新 `summary_offsets[turn_id]`
- `transcript_reset` 更新 `last_reset_offset`
- 每 N 条 record 或文件超过阈值后写 `transcript.checkpoint.json`，包含最近可恢复 `OutputTree` snapshot 和 offset
- replay 优先从 checkpoint/last_reset/last summary offset seek；只有索引缺失、损坏或 size 不匹配时才回退两遍扫描并重建 index
- index 写入采用 temp file + fsync + atomic replace，避免半写

当前实现不单独新增 replay 模块：replay、index 校验、checkpoint 恢复和索引重建都内聚在 `src/voidx/memory/transcript.py`。`load_transcript()` 优先从 checkpoint、summary offset 或 reset offset seek；只有 index 不存在、损坏或 size 不匹配时，才扫描 `transcript.jsonl` 并原子重写 `transcript.idx.json`。

#### 与现有代码的衔接

当前持久化入口已经下沉到 `GraphSessionRuntime.persist_transcript_snapshot()` / `restore_transcript_snapshot()`，`transcript_mixin.py` 只是代理。因此 JSONL 读写主路径在 `src/voidx/agent/graph/session_runtime.py` 与 `src/voidx/memory/transcript.py`：

```python
async def restore_transcript_snapshot(self, *, append=False):
    host = self.host
    if host._session is None:
        return False
    active_dock = host._ui.get_dock()
    if active_dock is None:
        return False

    rows = await load_transcript(host._session.id)  # JSONL-only
    if not rows:
        return False
    tree = transcript_rows_to_tree(rows)

    active_dock.restore_tree(tree, append=append)
    return True
```

`load_transcript()` 不再读取 SQLite transcript payload 表；索引缺失/损坏时只在 `transcript.jsonl` 内全量扫描并重建索引。

### 3.5 Subagent Parent-Child Chain

每个 subagent 独立 JSONL 文件，主 transcript 通过引用关联。

#### 主 transcript.jsonl

```jsonl
{"type":"node","turn_id":2,"node_id":5,"node_type":"subagent","agent_run_id":"agent_0","header":"🔍 explore","status":"running"}
{"type":"node_update","turn_id":2,"node_id":5,"status":"done","elapsed":3.2}
```

#### subagents/agent_0.jsonl

```jsonl
{"type":"subagent_start","parent_session_id":"abc123","parent_turn_id":2,"parent_node_id":5,"agent_name":"explore","timestamp":"..."}
{"type":"node","turn_id":0,"node_id":0,"node_type":"assistant","header":"assistant","status":"running"}
```

#### 恢复流程

1. 重放主 `transcript.jsonl`
2. 遇到 `node_type=subagent` 时，记录 `agent_run_id`
3. 如需完整恢复 subagent 内容，重放 `subagents/<agent_run_id>.jsonl`
4. 将 subagent tree 挂载到对应 parent node 下

#### 与现有代码的衔接

> **【实现状态 2026-06-15】** ✅ 已实现。主 transcript 已持久化 `agent_run_id` 字段，`append_subagent_event(session_id, agent_run_id, event)` 已写入 `sessions/<sid>/subagents/<agent_run_id>.jsonl`。`VoidXGraph._subagent_runner()` 写 `subagent_start` / `subagent_finish` 生命周期事件；`run_subagent()` 写 `assistant_message` 和 `tool_result` 子流。

当前 subagent 相关入口：

- `VoidXGraph._subagent_runner()` 分配递增 `agent_id`，事件模式下发出 `SubagentStarted(subagent_id=f"agent_{agent_id}")` / `SubagentFinished`
- `run_subagent()` 对 worker LLM 调用保存 `frame_kind="worker"` 且 `agent_persona=persona` 的 context frame
- `CaptureConsole` 会把子 agent 工具输出挂到父 turn/tree 下；事件消费者会创建 `node_type="subagent"` 且 `agent_run_id=e.subagent_id` 的节点
- `tree_to_transcript_rows()` 已持久化 `agent_run_id`

因此 JSONL 写入不能只靠“`agent_run_id` 不为空的节点路由到 subagent 文件”。更稳的衔接方式是：主 transcript 记录 `subagent` wrapper 节点和 parent-child 引用；subagent 细节文件从 `SubagentStarted` / `SubagentStepStarted` / `ToolStarted(agent_id=...)` 等 UI events 或 CaptureConsole 统一事件入口写入。这样不会依赖恢复时重新拆分已经扁平化的 `OutputTree`。

具体写入接口：

- `append_subagent_event(session_id, subagent_id, record)` 写 `sessions/<sid>/subagents/<subagent_id>.jsonl`
- `_subagent_runner()` 写 `subagent_start`，包含 `parent_tool_call_id`、`agent_name/persona`、`description`
- `run_subagent()` 写每步 `assistant_message`
- `run_subagent()` 写子工具 `tool_result`
- `_subagent_runner()` finally 写 `subagent_finish`，包含 `ok`、`elapsed`、`final_step`、`max_steps`、`finish_reason`

主 transcript 的 `subagent` node 只保存 wrapper 和 `agent_run_id`。完整细节延迟读取 `subagents/<agent_run_id>.jsonl`，避免主 transcript 继续膨胀。

### 3.6 文件版本历史

> **【Updated 2026-06-10】** 新增章节。对比 Claude Code 的 `file-history/` 目录，voidx 缺少文件修改的版本快照能力。
>
> **【实现状态 2026-06-15】** ✅ 已实现。`SessionChangeTracker` 有当前 turn 内存快照 + `/rollback`；`src/voidx/tools/file_state.py` 已新增 `save_file_version()`，写入 `file-history/` 快照和 `manifest.jsonl`，并在短 hash 碰撞时退到 full hash 文件名。`write`、`edit`、`lsp_format` 已在写入前保存原始内容；subagent `ToolContext` 已透传 `session_id`，避免子 agent 文件历史落到 `default` session。

#### 存储格式

```
sessions/<session-id>/file-history/
└── <content-hash>@v<N>              # 文件内容快照
```

- `content-hash`：文件路径的 SHA-256，文件名可用前 16 位短 hash，但必须在 manifest 中保存完整 hash
- `v<N>`：版本号，从 1 递增

#### 写入时机

当前已有 `SessionChangeTracker` 会在当前 turn 内为 `/rollback` 捕获内存快照；本节新增的是跨 session 的持久化 `file-history/`。在以下 tool 执行**之前**保存原始文件内容：

| Tool | 触发条件 |
|------|---------|
| `write` | 目标文件已存在 |
| `edit` | 目标文件已存在 |
| `lsp_format` | 目标文件已存在 |

#### 写入逻辑

```python
# src/voidx/tools/file_state.py 扩展

def save_file_version(ctx: ToolContext, path: Path) -> None:
    """Save a version snapshot of the file before modification."""
    if not path.exists() or path.is_dir():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    full_hash = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    short_hash = full_hash[:16]
    version = _next_version(ctx, full_hash)
    snapshot_path = (
        _session_dir(ctx.session_id) / "file-history" / f"{short_hash}@v{version}"
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(content, encoding="utf-8")
    _append_file_history_manifest(ctx, path, full_hash, short_hash, version)
    record_mtime(ctx, path)
```

必须同时维护 `file-history/manifest.jsonl`：

```jsonl
{"full_hash":"...","short_hash":"a1b2c3d4e5f6a7b8","version":1,"path":"src/app.py","snapshot":"a1b2c3d4e5f6a7b8@v1","created_at":"..."}
```

恢复时用 `full_hash + path` 校验，发现 short hash 碰撞时改用 `full_hash@v<N>` 文件名或给短文件名加后缀；不能只依赖 16 位短 hash。

#### 与 Claude Code 的对比

| 维度 | Claude Code | voidx |
|------|------------|-------|
| 存储位置 | `~/.claude/file-history/{session}/{hash}@v{N}` | `~/.voidx/sessions/{session}/file-history/{hash}@v{N}` |
| 触发时机 | 每次 tool call 后快照 | 每次 tool call 前保存原始内容 |
| 粒度 | 整个 tracked files 列表 | 只存被修改的文件 |
| 清理 | 随 session 删除 | 随 session 删除 |

#### 恢复能力

Phase 1 只做存储，不提供 undo 命令。后续可扩展 `/undo` 命令从 `file-history/` 恢复上一个版本。

现有 `/rollback` 应继续保留，作为当前 turn 的快速恢复能力。`file-history/` 是跨 session / resume 后可用的持久历史，不替代当前内存 rollback。

### 3.7 `/session` 命令体系

> **【实现状态 2026-06-15】** ✅ 已实现。`/session list/new/resume` 已落地，分别复用 `/list`、`/clear`、`/resume` 现有实现；`/session del --dry-run [7d|15d|30d|all]` 已落地：复用 `cleanup.py` 计算候选集合、输出预览、不删除数据。`/session del [7d|15d|30d|all]` 已接入交互确认，确认后调用同一候选集合执行删除。无参数 `/session del` 在有 app 选择器时先选择范围，无 app/dry-run 路径保留默认 `30d`。候选展示包含更新时间、workspace、title、message count 和总可回收空间；空间估算只包含 session 文件目录。

| 命令 | 功能 | 替代现有 |
|------|------|----------|
| `/session list` | 列出历史 session | `/list` |
| `/session new` | 新建空 session | `/clear` |
| `/session resume <id>` | 恢复指定 session | `/resume` |
| `/session del` | 删除默认范围（当前为 30 天前）的旧 session | **新增** |
| `/session del 7d` | 删除 7 天前的 session | **新增** |
| `/session del 15d` | 删除 15 天前的 session | **新增** |
| `/session del 30d` | 删除 30 天前的 session | **新增** |
| `/session del all` | 删除所有 session | **新增** |
| `/session del --dry-run 7d` | 只预览，不删除 | **新增** |

#### `/session del` 交互流程

当前实现无参数时在有 app 选择器的环境中弹出范围选择；无 app 或 dry-run 路径保留默认范围 `30d`：

```
Delete sessions older than:
  > 7 days
    15 days
    30 days
    All sessions
    Cancel
```

选择后显示预览：

```
Will delete 42 sessions (7 days old):
  - 12 empty sessions
  - 30 sessions with messages
  - Disk space to reclaim: ~23 MB
Confirm? [y/N]
```

`--dry-run` 只输出同样的预览和候选 session id，不弹确认、不删除 SQLite 或文件。交互式 `/session del` 内部也先调用同一候选计算函数，再进入 confirm/apply，保证预览和实际删除使用同一候选集合。

> **【Updated 2026-06-15】** 磁盘空间估算只遍历候选 session 文件目录；不再读取废弃 SQLite payload 表估算大小。

确认后执行删除：SQLite 行 + JSONL 文件 + context 文件 + file-history 文件，一条不留。

注意区分两类删除：

- 删除整个 session：删除 `sessions` 行并 `shutil.rmtree(sessions/<session-id>)`
- 保留 session 但清理消息：`clear_messages()`、`delete_messages_from()`、`delete_messages_through()` 追加 `message_deleted` / `session_cleared` 等 tombstone，并同步 `sessions.message_count`

#### 向后兼容

- `/clear` → `/session new` 别名，不破坏现有习惯
- `/list` → `/session list` 别名
- `/resume` → `/session resume` 别名

#### 命令注册

```python
# handler.py dispatch 新增
"/session": lambda: self._session_dispatch(args),
```

```python
# session.py
async def _session_dispatch(self, args: str) -> None:
    parts = args.strip().split(maxsplit=1)
    sub = parts[0] if parts else ""
    sub_args = parts[1] if len(parts) > 1 else ""

    match sub:
        case "list" | "ls" => await self._list_sessions()
        case "new" => await self._clear()
        case "resume" => await self._resume(f"/resume {sub_args}")
        case "del" | "delete" => await self._delete_sessions(sub_args)
        case _ => ui.print("[dim]Usage: /session list|new|resume|del[/dim]")
```

### 3.8 Session 清理实现

```python
# src/voidx/memory/cleanup.py

async def plan_session_delete(scope: str = "30d") -> SessionDeletePlan:
    ...

async def apply_session_delete_plan(plan: SessionDeletePlan) -> int:
    deleted = 0
    for candidate in plan.candidates:
        await delete_session(candidate.session_id)
        deleted += 1
    return deleted
```

`delete_session()` 当前已是统一入口：先删 session 目录，再删 SQLite 行，再清理 session lock。`apply_session_delete_plan()` 只是批量复用这条路径。

### 3.9 JSONL 写入器

> **【Updated 2026-06-10】** 修正并发模型。原设计使用全局 `_async_lock`，所有 session 共享一把锁，session A 写入会阻塞 session B。且在 `async with` 内做同步 IO 会阻塞事件循环。改为 per-session 锁 + `asyncio.to_thread()` 异步写入 + 写后 flush。

```python
# src/voidx/memory/jsonl_store.py

import asyncio
import json
import os
from pathlib import Path
from typing import Any

_session_locks: dict[str, asyncio.Lock] = {}
_session_lock_refs: dict[str, int] = {}
_locks_lock = asyncio.Lock()


def _session_dir(session_id: str) -> Path:
    return Path.home() / ".voidx" / "sessions" / session_id


async def _get_lock(session_id: str) -> asyncio.Lock:
    async with _locks_lock:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
        return _session_locks[session_id]


def _append_jsonl_sync(path: Path, records: list[dict[str, Any]]) -> None:
    """Synchronous JSONL append — called via asyncio.to_thread()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _write_jsonl_sync(path: Path, records: list[dict[str, Any]]) -> Path:
    """Synchronous JSONL write (overwrite) — called via asyncio.to_thread()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


async def append_transcript(session_id: str, records: list[dict[str, Any]]) -> None:
    """Append records to session transcript JSONL."""
    lock = await _get_lock(session_id)
    path = _session_dir(session_id) / "transcript.jsonl"
    async with lock:
        await asyncio.to_thread(_append_jsonl_sync, path, records)


async def append_context_frame(
    session_id: str, frame_id: str, messages: list[dict[str, Any]]
) -> Path:
    """Write context frame messages to JSONL, return file path."""
    lock = await _get_lock(session_id)
    dir_path = _session_dir(session_id) / "context"
    path = dir_path / f"{frame_id}.jsonl"
    async with lock:
        return await asyncio.to_thread(_write_jsonl_sync, path, messages)


async def append_subagent_transcript(
    session_id: str, agent_run_id: str, records: list[dict[str, Any]]
) -> None:
    """Append records to subagent transcript JSONL."""
    lock = await _get_lock(session_id)
    path = _session_dir(session_id) / "subagents" / f"{agent_run_id}.jsonl"
    async with lock:
        await asyncio.to_thread(_append_jsonl_sync, path, records)
```

`_session_locks` 不能无限增长。实现需要在 session 删除/clear cleanup 时调用 `drop_session_lock(session_id)`；也可以用 LRU/weakref 维护最近活跃锁。删除锁前必须确认没有正在持有的写入任务，或者只在 `_locks_lock` 下标记为可回收，等引用计数为 0 再移除。

## 4. 迁移计划

### Phase 1：JSONL 写入层

- 新增 `src/voidx/memory/jsonl_store.py`
- 新增 transcript index/checkpoint 写入：`transcript.idx.json` + 可选 `transcript.checkpoint.json`
- 修改 `session.py`：`save_message` 写 `messages.jsonl`，维护 `sessions.message_count`
- 修改 `session.py`：`clear_messages` / `delete_messages_from` / `delete_messages_through` 同时写 message/context/runtime tombstone
- 修改 `session_runtime.py`：`persist_transcript_snapshot` 写 transcript JSONL 主路径
- 修改 `context_frames.py`：`save_context_frame` 写轻量 SQLite 索引 + JSONL 文件
- 修改 `runtime_state.py`：runtime 恢复主路径写 `session_runtime_state`，debug 路径写 `runtime_debug.jsonl`
- 修改 `file_state.py`：write/edit/lsp_format 前保存持久文件版本快照，并保留当前 `ui/session.py` 内存 rollback
- transcript replay/index/checkpoint 逻辑落在 `src/voidx/memory/transcript.py`

验证闸门：

- save/load parity：同一 session 的 `messages.jsonl`、`content_format`、tool call 字段完全一致
- transcript payload boundary：message-backed transcript 节点只写 `message_id` / `tool_call_id` / preview / UI metadata，不复制完整 message content 或 tool result
- tombstone parity：`delete_messages_from()` / `delete_messages_through()` 同时写 message/context/runtime 删除记录
- clear parity：`clear_messages()` 同时清理轻量索引/最新态/计数，并写入 message/transcript/context/runtime reset marker
- transcript index：append transcript 后 `transcript.idx.json` size/offset 与文件匹配，索引损坏时能回退扫描并重建

### Phase 2：JSONL 读取层 + DB 路径

- 修改 `load_messages`：从 `messages.jsonl` 读取
- 修改 `GraphSessionRuntime.restore_transcript_snapshot`：从 JSONL index/checkpoint 重放
- 修改 `load_context_frames`：从 JSONL 文件读取 messages，SQLite 只查索引
- 修改 `store.py`：DB 路径改为 `~/.voidx/store/voidx.db`；不迁移、不读取旧 `~/.voidx/voidx.db`
- 修改 `runtime_state.py`：`message_runtime_snapshots` 合并到 `session_runtime_state`，debug 读取走 `runtime_debug.jsonl`
- 修改 `list_sessions` / `get_session`：读取 `sessions.message_count`，不再 JOIN `messages`

验证闸门：

- replay equivalence：同一 transcript 从 JSONL replay 后 OutputTree 结构一致；message-backed 节点按引用 join `messages.jsonl` 补齐展示文本
- message count：`list_sessions()` / `get_session()` 不 JOIN `messages`，计数可从 JSONL 重算修复
- DB path：旧根路径 DB 存在或损坏时不影响新 store DB 初始化；新旧 DB 同时存在时只读新 DB
- runtime resume：resume 只依赖 `session_runtime_state` 最新状态，per-message debug 查询不误读最新状态

### Phase 3：命令体系 + 清理

- 新增 `/session` 命令命名空间
- 实现 `/session del` 交互式清理和 `/session del --dry-run`
- 旧命令 `/list` → `/session list`，`/clear` → `/session new` 别名

验证闸门：

- dry-run 不删除 SQLite 行和 session 目录，并输出候选 session id、数量、空间估算
- 交互确认和 apply 使用 dry-run 同一候选集合，避免预览/实际删除漂移
- session 删除失败时不提前删除 DB 索引；DB 删除失败时可由下一次 cleanup 幂等修复
- `/clear` 作为 `/session new` 别名时不污染旧 session 的 `messages.jsonl`
- `transcript.log` 不作为 `/session del --dry-run` 的空间估算或删除正确性来源；若存在，只随日志策略或 legacy cleanup 处理

### Phase 4：移除 SQLite session 级表

- 新 schema 不创建 `messages` / `turns` / `transcript_nodes` / `message_runtime_snapshots`
- `context_frames.messages_json` 不再保存 payload，完整 frame messages 只写 JSONL
- `sessions.message_count` 成为 list/resume 的唯一消息数量索引
- 如确需旧数据转出，提供离线导出脚本，不挂入运行时启动路径
- 提供 Phase 4 回滚方案：通过 schema version 和外部备份/恢复处理；运行时代码不从旧 SQLite payload 表 fallback

验证闸门：

- offline export：如保留离线脚本，旧 DB 能导出 messages/transcript/context/runtime debug 到 JSONL/索引文件；该脚本不在启动路径调用
- corruption drill：迁移后人为破坏 JSONL/index，启动路径能报告错误或重建 index，不从 SQLite payload 表恢复
- schema cleanup：新库不创建旧 payload 表；已有库的物理 DROP 可由独立迁移脚本处理
- size gate：样本 DB 迁移后主 DB 只剩轻量索引和配置，目标 < 1 MB

## 5. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/voidx/memory/jsonl_store.py` | 新增 | JSONL append-only 写入器（per-session 锁 + async IO） |
| `src/voidx/memory/cleanup.py` | 新增 | Session 删除候选计算 + apply；dry-run 与实际删除复用同一候选集合 |
| `src/voidx/memory/session.py` | 修改 | `save_message` → JSONL append，`load_messages` → JSONL 读取，delete/clear tombstone |
| `src/voidx/memory/transcript.py` | 修改 | `replace_transcript` 写 JSONL + index，`load_transcript` 只读 JSONL，支持 `node_update` 合并语义 |
| `src/voidx/memory/context_frames.py` | 修改 | DB 只保留轻量索引，完整 messages 写 `context/<id>.jsonl`，支持 `context_frame_deleted` |
| `src/voidx/memory/runtime_state.py` | 修改 | `message_runtime_snapshots` 停写；最新态写 `session_runtime_state`，debug 历史读写 `runtime_debug.jsonl` |
| `src/voidx/memory/store.py` | 修改 | DB 路径改为 `~/.voidx/store/`，schema 变更 |
| `src/voidx/tools/file_state.py` | 修改 | 新增 `save_file_version()` 文件版本快照、manifest 写入、短 hash 碰撞规避 |
| `src/voidx/tools/file_ops.py` | 修改 | `write` / `edit` 成功写入前保存原始内容快照 |
| `src/voidx/tools/lsp.py` | 修改 | `lsp_format` 调用 formatter 前保存原始内容快照 |
| `src/voidx/ui/session.py` | 已有 | 保留当前 turn 内存 rollback，不承担持久 file-history 写入 |
| `src/voidx/agent/graph/session_runtime.py` | 修改 | transcript JSONL 读写；message-backed 节点写引用不写完整 payload |
| `src/voidx/agent/graph/transcript_mixin.py` | 修改 | 如有需要仅保留代理调用 |
| `src/voidx/agent/graph/turn_runner.py` | 修改 | `save_message` → JSONL append |
| `src/voidx/agent/slash/session.py` | 修改 | `/session` 命令体系 |
| `src/voidx/agent/slash/handler.py` | 修改 | 注册 `/session` 子命令 |
| `src/voidx/ui/commands.py` | 修改 | `/session` 命令注册 |
| `src/voidx/ui/transcript.py` | 修改 | 支持从 JSONL replay 结果构建 tree |
| `src/voidx/ui/output/events/consumers.py` / `capture.py` | 修改 | subagent transcript JSONL 写入的事件入口 |

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| JSONL 文件损坏 | 逐行解析，跳过坏行；transcript index 缺失/损坏时扫描重建 |
| 迁移期间数据不一致 | 以 JSONL 为唯一 payload source of truth |
| append-only 与删除语义冲突 | `message_deleted` / `session_cleared` 作为 tombstone，loader 统一应用 |
| 删除级联遗漏 context/runtime | `message_deleted`、`context_frame_deleted`、`runtime_state_deleted` 必须同入口写入，并用 parity 测试覆盖 |
| transcript 重复保存 message payload | transcript schema 限定为 `message_id` / `tool_call_id` 引用 + UI metadata；完整 content 只读 `messages.jsonl` |
| 文本 log 被误当 session 存储 | `transcript.log` 和普通 logging 明确不参与 replay/resume/迁移，按 debug 日志策略 rotate/清理 |
| `message_count` 与 JSONL 不一致 | 所有 save/delete/clear 入口同步更新索引并可幂等重算；测试覆盖 list/resume 计数 |
| 大 session JSONL 重放性能 | `transcript.idx.json` + checkpoint seek 是主路径；二遍扫描只在索引缺失/损坏时 fallback 并重建 |
| `/session del` 误删 | dry-run 预览 + 交互确认；预览和 apply 复用同一候选集合 |
| 并发写入 JSONL | per-session asyncio.Lock + `asyncio.to_thread()` 避免阻塞事件循环 |
| per-session lock 字典泄漏 | session cleanup 时回收 lock，或使用 LRU/weakref + 引用计数 |
| 旧版本无法读取新格式 | 不保留运行时代码 fallback；通过版本和备份策略处理 |
| 文件版本历史占用磁盘 | 随 session 删除一起清理；单个快照通常 < 100 KB |
| DB 路径变更 | 运行时只打开 `~/.voidx/store/voidx.db`；旧根路径 DB 原样忽略 |
| Phase 4 回滚困难 | schema version + 外部备份/恢复策略；不在运行时保留旧表 fallback |
| 多进程写锁竞争 | 重构后 SQLite 只写小行（session 元数据、runtime state），竞争基本消除 |

## 7. 预期收益

| 指标 | 当前 | 重构后 |
|------|------|--------|
| DB 文件大小 | 87 MB（清理前 906 MB） | < 1 MB（只有全局配置和索引） |
| DB 膨胀主因 | `context_frames.messages_json` 53 MB (61%) + `messages` 7 MB (8%) | 全部移出，改用 JSONL 文件 |
| DB 路径 | `~/.voidx/voidx.db` | `~/.voidx/store/voidx.db` |
| Message 存储 | SQLite `messages` 表 | `messages.jsonl` append-only |
| Transcript 持久化 | DELETE + INSERT 全量，可能重复 UI 文本 | Append-only UI 事件 + message/tool 引用 |
| 文本 transcript log | 可作为额外对话日志存在 | 退出目标 session 存储，只保留 legacy/debug 角色 |
| Context frame 存储 | SQLite BLOB | JSONL 文件，按需加载 |
| Runtime state | `message_runtime_snapshots` 累积 | 合并到 `session_runtime_state`，每 session 单行替换 |
| Session 清理 | 手动操作 DB | `/session del` 交互式 |
| Subagent 隔离 | 混在主 transcript | 独立 JSONL + parent-child chain |
| 文件版本历史 | 仅当前 turn 内存 rollback | `file-history/` 持久快照，支持后续跨 session undo |
| 崩溃安全 | WAL 但全量替换风险 | Append-only + fsync，单行损坏可跳过 |
| 多进程写锁竞争 | 严重（context_frame 0.5MB/次） | 基本消除（SQLite 只写小行） |

## 8. 与 Claude Code 存储架构对比

> **【Updated 2026-06-10】** 新增章节。基于对 Claude Code `~/.claude/` 目录的实测对比。

| 维度 | Claude Code | voidx（重构后） | 评价 |
|------|------------|----------------|------|
| 存储格式 | 纯 JSONL（无 SQLite） | JSONL + SQLite 混合 | ✅ 混合方案更适合 voidx：大 payload 走 JSONL，list/resume/message_count/context frame 索引用 SQLite |
| 项目隔离 | `projects/{encoded-path}/` 按项目分目录 | `sessions/{session-id}/` 按 session 分 | ⚠️ 缺项目级隔离，但 session 级够用 |
| 文件历史 | `file-history/{session}/{hash}@v{N}` | `file-history/{hash}@v{N}` | ✅ 方案一致 |
| Subagent | `{session}/subagents/agent-*.jsonl` | `subagents/<agent-run-id>.jsonl` | ✅ 方案一致 |
| 清理 | 无内置命令 | `/session del` 交互式 | ✅ 比 Claude Code 更好 |
| Context frame | 不存（无此概念） | JSONL 文件 + SQLite 索引 | ✅ 合理，voidx 特有需求 |
| 插件 | `plugins/` 完整插件系统 | 无 | — 不在本次范围 |
| 遥测 | `telemetry/` | 无 | — 不在本次范围 |
| Shell 快照 | `shell-snapshots/` | 无 | — 不在本次范围 |
