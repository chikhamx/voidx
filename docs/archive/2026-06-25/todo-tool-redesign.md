> **Status: Done**

# Todo Tool Redesign — 技术设计文档

## Context

当前 `TodoWriteTool` 采用全量替换模式——每次调用都要传入完整的 todo 列表，LLM 想改一个 item 的状态也得重传全部。同时 `TaskState.todo_state` 挂载完整的 `items` 列表并逐条注入 system prompt，浪费 token 且在 compaction 后 LLM 容易丢失上下文（数字 id 无语义）。

本方案引入 `op` 参数（`write`/`update`/`read`）和字符串语义化 id，使 LLM 可以增量操作 todo、按需查询，并将 TaskState 上的 todo 简化为摘要元数据。

## Goals and Non-Goals

### Goals

- 引入 `op` 参数，支持 `write`（全量写入）、`update`（按 id 增量更新）、`read`（按 filter 只读查询）
- 使用字符串语义化 id（如 `"schema"`, `"api"`），compaction 后 LLM 零成本回忆
- `TaskState.todo_state` 简化为摘要元数据（计数 + 仅 in_progress 的 `active_items`）
- `read` 操作不产生副作用（不更新 TaskState、不触发 UI 事件、不 replay sanitize）
- `id` 在 `TodoItem`/`TodoRunItem`/`TodoItemPayload`/`DockTodoItem` 各层贯通，UI 可展示语义化 id

### Non-Goals

- 不引入持久化存储（todo 仍然存在内存 `TaskTracker` 中）
- 不支持跨 session 的 todo 恢复
- 不支持 todo item 的删除操作（通过 `write` 全量重写替代）
- 不考虑老数据兼容——现有无 id 的 todo 调用方式不做迁移，直接改为 id 必填

## Architecture

### 数据流（改造后）

```
LLM 调用 todo(op=write|update|read)
        |
        v
  TodoWriteTool.execute()
        |
  +-----+-----------------------------+
  |     |  write        update         |   read
  |     v              v               |      v
  |  TaskTracker     TaskTracker       |   只读返回
  |  .set_todos()    .update()         |   不修改存储
  |     |              |               |
  |     v              v               |
  |  todo_run_state_from_result()      |
  |     |              |               |
  |     v              v               |
  |  TodoRunState (摘要)               |   <-- 不触发
  |     |              |               |
  |     +-> TaskState.todo_state       |
  |     +-> UI: TodoUpdated event      |
  |     +-> Replay sanitize            |
  +------------------------------------+
        |
        v
  runtime_context 注入 system prompt
  (单行摘要 + active_items)
```

### 关键模块边界

| 层 | 职责 |
|---|------|
| `tools/todo.py` | 参数解析、op 分发、ToolMessage 构建 |
| `tools/task_tracker.py` | 内存存储：dict[str, dict]，O(1) 查找/更新 |
| `runtime/task_state.py` | `TodoRunState`/`TodoRunItem` 模型定义（摘要层）。注：`agent/task_state.py` 仅为 re-export 兼容层，实际定义在此 |
| `agent/todo_state.py` | result -> TodoRunState 转换；replay sanitize 逻辑 |
| `agent/runtime_context.py` | TodoRunState -> system prompt 注入（遍历 `active_items` 而非 `items`） |
| `agent/graph/subagent.py` | 子 agent 工具执行后 todo_state_sink 调度（判断条件从 `items` 改为 `active_items` 或计数） |
| `agent/graph/runtime_guards.py` | 循环检测签名（改用计数字段，见 [Impact Analysis](#impact-analysis)） |
| `agent/graph/tool_executor/workflow.py` | state patch：判断 `todo_state` 是否非空写入 update（条件从 `items` 改为计数 > 0） |
| `agent/graph/todo_events.py` | result -> UI TodoUpdated 事件（payload 带 id） |
| `memory/runtime_state.py` | todo_state 序列化/反序列化（`_dump_todo_state`/`_load_todo_state` 适配新字段） |
| `ui/output/events/schema.py` | `TodoItemPayload` 加 `id` 字段 |
| `ui/output/dock/todo.py` | `DockTodoItem` 加 `id` 字段；渲染时展示 id 前缀 |

## Data Model

### TodoInput（工具输入）

```
TodoInput
+-- op: Literal["write", "update", "read"] = "write"
+-- todos: list[TodoItem] | None           (write 时必填)
+-- updates: list[TodoUpdateOp] | None     (update 时必填)
+-- filter: TodoReadFilter = "all"         (read 时使用)

TodoItem
+-- id: str (max_length=20, required)      (LLM 自分配语义化 id，write 时必填)
+-- content: str
+-- status: TodoStatus = "pending"

TodoUpdateOp
+-- id: str                                (目标 item id)
+-- status: TodoStatus                     (新状态)
+-- content: str | None = None             (可选：同时改描述)

TodoReadFilter = Literal["all", "pending", "in_progress", "completed", "done"]
# "done" 是聚合 filter，匹配 status 为 "completed" 或 "cancelled" 的项（OR 语义）

TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]
```

### TodoRunState（TaskState 上的摘要）

```
TodoRunState (改造后)
+-- summary: str = ""               (例: "1/3 done . 1 active . 1 pending")
+-- total: int = 0
+-- done: int = 0
+-- in_progress: int = 0
+-- pending: int = 0
+-- cancelled: int = 0
+-- active_items: list[TodoRunItem] = []  (仅 in_progress 项)
+-- updated_at: str = ""

TodoRunState (改造前，对比)
+-- summary: str
+-- items: list[TodoRunItem] = []   <-- 完整列表，改为 active_items
+-- updated_at: str
```

### TaskTracker 存储

```
TaskTracker
+-- _todos: dict[str, dict]         <-- 从 list 改为 dict，key = id
|   例: {"schema": {"content": "...", "status": "completed"}, ...}
+-- 新增方法:
    +-- update_todos(updates: list[dict])  按 id 增量更新
    +-- get_todos() -> dict[str, dict]     返回完整 dict（read 用）
```

## API Contract

### TodoWriteTool.execute()

- **Signature**: `async def execute(self, args: dict, ctx: ToolContext) -> ToolResult`
- **Input**: `TodoInput` (validated from args)
- **Output**: `ToolResult` (含 title, output, summary, metadata)

#### op=write

- **Request**:
  ```json
  {"op": "write", "todos": [
      {"id": "schema", "content": "设计数据库 schema", "status": "in_progress"},
      {"id": "api",    "content": "实现 API 接口",      "status": "pending"}
  ]}
  ```
- **Response**: 全量替换，返回写入后的完整列表
  ```
  Written 2 items.
    [in_progress] schema: 设计数据库 schema
    [pending] api: 实现 API 接口
  Summary: 0/2 done . 1 active . 1 pending
  ```
- **Metadata**:
  ```json
  {
    "total": 2, "done": 0, "in_progress": 1, "pending": 1, "cancelled": 0,
    "todo_items": [{"id": "schema", "content": "...", "status": "in_progress"}, ...],
    "todo_summary": "0/2 done . 1 active . 1 pending"
  }
  ```

#### op=update

- **语义**：非原子逐项更新。id 不存在的项静默跳过，其余项正常生效。
- **Request**:
  ```json
  {"op": "update", "updates": [
      {"id": "schema", "status": "completed"},
      {"id": "api",    "status": "in_progress"},
      {"id": "ghost",  "status": "completed"}
  ]}
  ```
- **Response**：返回更新后的完整列表，缺失 id 在响应中提示
  ```
  Updated 2 items. Skipped unknown ids: ghost
    [completed] schema: 设计数据库 schema
    [in_progress] api: 实现 API 接口
  Summary: 1/2 done . 1 active . 0 pending
  ```
- **Metadata**: 同 write，额外含 `warnings` 字段列出跳过的 id
  ```json
  {
    "total": 2, "done": 1, "in_progress": 1, "pending": 0, "cancelled": 0,
    "todo_items": [...],
    "todo_summary": "1/2 done . 1 active . 0 pending",
    "warnings": ["Skipped unknown ids: ghost"]
  }
  ```

#### op=read

- **Request**:
  ```json
  {"op": "read", "filter": "pending"}
  ```
- **Response**: 按 filter 过滤后的列表
  ```
  Todo pending (1 item):
    [pending] api: 实现 API 接口
  Summary: 1/2 done . 1 active . 0 pending
  ```
- **Metadata**: 包含计数字段和 `todo_summary`，但 **不包含 `todo_items` 键**。这样 `todo_run_state_from_result()` 在 read 操作时返回 `None`（因 `raw_items` 为 None），下游 executor/workflow 不会将其作为 state patch 写入 TaskState。
  ```json
  {
    "total": 2, "done": 1, "in_progress": 1, "pending": 0, "cancelled": 0,
    "todo_summary": "1/2 done . 1 active . 0 pending",
    "todo_op": "read"
  }
  ```
- **副作用**: 无。不更新 TodoRunState、不触发 UI 事件、不 replay sanitize。`metadata.todo_op = "read"` 作为显式标记，供下游区分。

### 执行层读操作静默（补充）

当前设计只在 `TodoWriteTool.execute()` 层面定义了 `read` 无副作用，但执行层仍有显式副作用路径：
- `agent/graph/tool_executor/executor.py:192-201`：`tid == "todo"` 时会尝试构造 `todo_updated_event(result)`，构造结果为 `None` 时还会发送 `WarningAppended`。
- `agent/graph/subagent.py:273-275`：子 agent 也会在 `tid == "todo"` 时读取 `todo_run_state_from_result(result)`。

如果只靠“metadata 不含 `todo_items`”来静默，`read` 调用会进入 `None` 分支并产生“Todo update ignored: tool returned malformed metadata.”的误告警。建议补充以下实现要求：

- 所有消费 `todo` tool result 的位置，优先读取 `metadata.todo_op`，当 `todo_op == "read"` 时直接跳过 `TodoUpdated`、`todo_state_sink`、`runtime_context` 更新和 replay sanitize。
- `todo_run_state_from_result(result)` 增加对 `todo_op` 的短路逻辑：当 `todo_op == "read"` 时直接返回 `None`，不再依赖是否缺少 `todo_items`。

### update 操作语义（补充）

`update` 采用非原子逐项更新：

- id 存在的项正常更新，id 不存在的项静默跳过。
- 响应文本中追加 `Skipped unknown ids: {ids}` 提示，metadata 中含 `warnings` 列表。
- LLM 可根据 warnings 决定是否重新 write 补充缺失项，无需因一个无效 id 重试整批。

### UI payload 一致性（补充）

当前 `TodoItemPayload` 与 `DockTodoItem` 都没有 `id` 字段，且 `DockTodoState` 的 payload 约定是 `content/status` 二元组。补充建议：

- `TodoItemPayload` 增加 `id: str`。
- `TodoUpdated` payload 建议增加可选 `todo_op: str`，方便 UI 事件总线区分 write/update/read。
- `DockTodoItem` 增加 `id: str`，并在 `render_todo_state_lines()` 中以 `{id}: {content}` 形式展示。
- `todo_state_payload()` 同步输出 `id`，避免 UI 层出现“事件有 id，dock payload 没有 id”的分裂。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| write 时 id 重复 | 返回错误 + 列出重复 id，不修改存储 |
| update 时 id 不存在 | 静默跳过该项，响应文本 + metadata.warnings 提示缺失 id |
| update 的 updates 为空 | 返回当前摘要（等同于 read all） |
| id 超过 20 字符 | 返回错误，提示缩短 id |
| read 无匹配 filter 的项 | 返回 "No items match filter: {filter}" + Summary |
| write 时 todos 为空或 None | 返回错误，提示至少提供一个 item |
| update 时 updates 为 None | 返回错误，提示提供 updates |
| read 时 todo 列表为空 | 返回 "Todo list is empty." |
| 存储层（TaskTracker）异常 | 返回通用错误，不影响主流程 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 字符串语义化 id | A: 系统分配数字 id; B: LLM 自分配字符串 id | compaction 后 LLM 看到 "schema" 就知道是什么，数字 id 需要先 query；LLM 生成短标识符零成本 |
| write 替代 create | create, replace, set | write 语义最准确：全量写入，符合文件操作隐喻 |
| read 带 filter 参数 | 独立 op（read_pending, read_done） | 一个 op + filter 参数更灵活，LLM 少记一组 op 名称 |
| done = completed + cancelled | 仅 completed | 终态合集更实用，LLM 关心"已结束"的项 |
| active_items 不设上限 | 上限 3 条 | 实际场景中 in_progress 项数量有限，截断反而丢失信息；system prompt 膨胀风险由 LLM 自行控制（不会同时开太多任务） |
| update 非原子逐项更新 | 原子整批失败 | LLM 无需因一个无效 id 重试整批；warnings 提示缺失 id，LLM 可自行决定是否补救 |
| read 操作通过 `todo_op` 短路 | 仅靠 metadata 缺字段静默 | 显式标记比隐式缺失更可靠，避免下游误报 "malformed metadata" 告警 |
| UI payload 贯通 id 字段 | 仅工具层有 id | 事件总线和 dock 渲染都需要 id 来关联展示，分裂会导致 UI 无法展示语义化 id |
| TaskTracker 用 dict 存储 | 保持 list + 线性查找 | dict O(1) 查找/更新，id 天然做 key |

## Impact Analysis

`TodoRunState.items` → `active_items` 改造影响所有消费 `items` 字段的下游模块。逐一说明改造方式：

### 1. `agent/runtime_context.py` — system prompt 注入

**现状**（`runtime_context.py:271-274`）：
```python
if todo_state is not None and todo_state.items:
    lines.append(f"- Active todo: {len(todo_state.items)} items")
    for item in todo_state.items:
        lines.append(f"  - {item.status}: {item.content}")
```

**改造**：遍历 `active_items`（仅 in_progress），并展示 id：
```python
if todo_state is not None and todo_state.active_items:
    lines.append(f"- Active todo: {todo_state.summary}")
    for item in todo_state.active_items:
        lines.append(f"  - [{item.id}] {item.status}: {item.content}")
```

### 2. `agent/graph/runtime_guards.py` — 循环检测签名

**现状**（`runtime_guards.py:352-372`）：`todo_status_signature` 遍历 `todo_state.items` 所有 item 的 status，做 `Counter` 签名。

**问题**：改为 `active_items` 后，签名只覆盖 in_progress 项，`pending → completed` 的变化不会反映在签名里，循环检测漏报。

**改造**：签名改用 `TodoRunState` 的计数字段，覆盖全部状态：
```python
def todo_status_signature(todo_state: Any) -> tuple[int, int, int, int]:
    """(done, in_progress, pending, cancelled) 计数签名。"""
    if todo_state is None:
        return (0, 0, 0, 0)
    return (
        getattr(todo_state, "done", 0),
        getattr(todo_state, "in_progress", 0),
        getattr(todo_state, "pending", 0),
        getattr(todo_state, "cancelled", 0),
    )
```

### 3. `agent/graph/subagent.py` — todo_state_sink 调度

**现状**（`subagent.py:274`）：
```python
if todo_state_sink is not None and todo_state is not None and todo_state.items:
    todo_state_sink(todo_state)
```

**问题**：改为 `active_items` 后，若子 agent 只做了 `completed` 更新（无 in_progress 项），`active_items` 为空，sink 不触发，父 agent 看不到状态变化。

**改造**：判断条件改为基于计数，只要 `total > 0` 就 sink：
```python
if todo_state_sink is not None and todo_state is not None and todo_state.total > 0:
    todo_state_sink(todo_state)
```

### 4. `agent/graph/tool_executor/workflow.py` — state patch

**现状**（`workflow.py:39-43`）：
```python
if item.tool_call.get("name") == "todo" and item.todo_state is not None:
    if item.todo_state.items:
        update["todo_state"] = item.todo_state.model_dump(mode="json")
    else:
        update["todo_state"] = None
```

**改造**：条件从 `items` 改为 `total > 0`。同时需区分 read 操作——read 的 ToolResult 不带 `todo_items` metadata，`todo_run_state_from_result` 返回 `None`，`item.todo_state` 自然为 `None`，不会进入此分支。

### 5. `memory/runtime_state.py` — 序列化

**现状**（`runtime_state.py:168-171`）：
```python
def _dump_todo_state(todo_state: TodoRunState | None) -> str:
    if todo_state is None or not todo_state.items:
        return ""
    return json.dumps(todo_state.model_dump(mode="json"), ensure_ascii=False)
```

**改造**：判断条件改为 `total > 0`。`_load_todo_state` 用 `TodoRunState.model_validate` 自动适配新字段，无需额外改动。

### 6. UI 层 — id 贯通

| 文件 | 改造 |
|------|------|
| `runtime/task_state.py` | `TodoRunItem` 加 `id: str` 字段 |
| `ui/output/events/schema.py` | `TodoItemPayload` 加 `id: str` 字段 |
| `agent/graph/todo_events.py` | 构造 payload 时传入 `id=item.id` |
| `ui/output/dock/todo.py` | `DockTodoItem` 加 `id: str`；`render_todo_state_lines` 渲染 `{id}: {content}` |
| `ui/output/events/consumers.py` | `set_todo_state` 透传 id（无需改签名，items 已是序列） |

## Test Plan

现有 3 个 todo 测试文件基于「全量替换 + 无 id」模型，需全部适配：

| 测试文件 | 改造内容 |
|---------|---------|
| `tests/test_agent/test_execute_tools_todo.py` | todo 相关 fixture 和断言适配 id 必填、op 分发、update/read 新行为 |
| `tests/test_agent/test_todo_events.py` | `TodoUpdated` payload 断言加 id 字段 |
| `tests/test_ui/gateway/test_ui_events_todo.py` | UI 事件 schema 断言加 id 字段 |

新增测试覆盖：
- `op=write` 带 id 的全量写入 + id 重复检测
- `op=update` 按 id 增量更新 + id 不存在时静默跳过 + warnings 提示
- `op=read` 各 filter（含 `done` 聚合语义）+ 无副作用验证（TaskState 不变、无 UI 事件）
- `todo_op == "read"` 时 `todo_run_state_from_result` 返回 None（短路验证）
- `todo_status_signature` 用计数字段的循环检测
- `runtime_context` 注入只含 `active_items` + summary


## Open Questions
- [ ] LLM prompt 中需要更新 todo 工具的 description，明确 op 语义和 id 规范，确保 LLM 能正确使用
- [ ] compaction guide 是否需要提示 LLM 在 compaction 后优先 read todo 再继续工作
