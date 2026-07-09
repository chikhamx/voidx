# Context Incremental Compiler — 增量上下文编译设计文档

> **Status: Done**

Date: 2026-06-06

## 1. 背景与动机

### 1.1 当前流程

每轮对话都会从头构建一份 LLM context：

```text
Turn N:
  1. session_msgs = list(self._session_msg_cache)
  2. msgs = messages_from_rows(session_msgs)
  3. msgs.append(turn_msg)
  4. _maybe_compact(msgs, ...)
  5. graph.ainvoke(initial, ...)
     5a. _prepare_with_stream:
         - await self._instruction.system()
         - await self._instruction.skill_context_for(...)
         - RuntimeContextBuilder(...).build()
         - context.apply_to_messages(state["messages"])
     5b. _call_llm -> stream_llm -> API call
     5c. _execute_tools -> tool calls
     5d. loop until no tool calls
  6. _compaction.prune(final["messages"])
  7. persist new AI/Tool messages
```

这条路径的语义大体正确：系统 prompt 和当前 turn overlay 都只在 LLM 调用前编译，用户原始消息在编译前持久化，历史用户消息不会保存上轮 task state。

问题在于编译成本每轮重复支付：历史消息反复 hydrate、稳定 system sections 反复 render、AGENTS.md / SKILL.md 文件反复读取和解析。

### 1.2 问题

| 问题 | 影响 |
|------|------|
| 每轮全量 hydrate 历史消息 | `messages_from_rows` 对所有 `MessageRow` 重新构造 LangChain message；结构化 content 还会重复 JSON parse |
| 每轮重读 instruction 文件 | `InstructionService.system()` 每轮重新读 AGENTS.md；skill discovery 也会重新读取 SKILL.md |
| 每轮重建稳定 sections | Base System、Role Prompt、Tool Contract、Workspace Facts、Project Facts、Session Date、Long Summary 大多数轮次不变 |
| 动态 overlay 缺少显式边界 | 当前靠约定只 prepend 最新用户消息；增量化后必须把这个约束写成 compiler 不变量 |
| subagent 复制的是已编译 parent messages | `_current_messages` 来自 LLM state，最新用户消息可能已经带父 agent 的 Runtime State / Active Skills / Task State |

### 1.3 目标

1. **增量化本地 context compiler**：稳定部分只在输入变化时重建，每轮只编译当前 turn overlay 和最新用户消息。
2. **复用 raw semantic history**：历史 `MessageRow -> BaseMessage` 结果按 row fingerprint 缓存，避免每轮全量 hydrate。
3. **保持动态语义正确**：Runtime State、Current DateTime、Active Skills、Current Task State 每轮迁移到最新用户消息，不保存到历史。
4. **缓存文件资产，不缓存激活结果**：AGENTS.md / SKILL.md body 可以按文件状态缓存；skill activation set 仍按最新 user / agent / intent / mode / scope 每轮计算。
5. **修正 subagent parent context**：subagent 只能继承 raw semantic parent history，不能继承父 agent 已编译的 turn overlay。

### 1.4 非目标

- 不把 Python `SystemMessage` 对象身份作为 provider prompt cache 的依据。Provider 只看到序列化请求内容，本设计主要优化本地构建成本。
- 不把 task overlay 持久化到 DB，也不把已编译消息作为下一轮 raw history。
- V1 不优化 tool execution 和 LLM streaming 的运行时逻辑。

---

## 2. 目标编译模型

### 2.1 Compiled Frame 顺序

目标 LLM frame：

```text
SystemMessage:
  Stable Prefix
  - Base System
  - Role Prompt
  - Mode Prompt
  - Tool Contract
  - Workspace Facts
  - Project Facts
  - Session Date
  - Long Summary

Historical semantic messages:
  - old user
  - old assistant
  - old tool
  - ...

Latest HumanMessage:
  Turn Overlay
  - Runtime State
  - Current DateTime
  - Active Skills
  - Current Task State

  User Message
  - latest user input
```

核心不变量：

> Persisted history remains raw semantic conversation. The compiler applies the current turn overlay only to the latest user message. Previous overlays are never persisted or replayed as history.

历史消息必须排在动态 overlay 前面。动态 overlay 是当前 turn 的执行上下文，不是历史事实。

### 2.2 分层与变化来源

| 层 | 内容 | 变化触发 | 编译策略 |
|----|------|----------|----------|
| Stable Prefix | Base System、Role、Mode、Tool Contract、Workspace Facts、Project Facts、Session Date、Long Summary | agent/mode 切换、workspace 切换、AGENTS.md 修改、session 切换、compaction summary 更新 | hash key 不变时复用 rendered system content |
| Raw History | 已持久化 user/assistant/tool messages | 新消息追加、compaction 删除、cancel 回滚、session 切换 | 按 row fingerprint 增量 hydrate |
| Turn Overlay | Runtime State、Current DateTime、Active Skills、Current Task State | 每轮 user / state / runtime 输入变化 | 每轮重新编译并 prepend 到最新 user message |
| Latest User | 当前用户输入和附件 payload | 每轮新输入 | raw message 进入 history cache 前不带 overlay |

### 2.3 动态字段清单

`Turn Overlay` 中包含所有不能沉淀到历史的内容：

- `Runtime State`: workspace、model/provider、interaction mode、permission profile、execution policy、agent、agent_id、user profile。
- `Current DateTime`: 每轮生成。
- `Active Skills`: 本轮激活的 full skill body，取决于 latest user、agent、task intent、interaction mode、scope、turn count。
- `Current Task State`: intent、intent resolution、pending approval、goal、goal phase/status/count、available tool ids、skill run state、user language/tone preference、permission gate。

其中 `Runtime State` 不一定每轮变化，但仍属于 current turn overlay。它可以按 rendered hash 判断是否复用字符串，但不能进入 raw history。

---

## 3. 设计方案

### 3.1 ContextCompilerCache

在 `VoidXGraph` 上维护一个 context compiler cache。这个 cache 只缓存可复用的构建产物，不缓存已编译 frame 作为历史。

```python
@dataclass
class RowMessageCacheEntry:
    fingerprint: str
    message: BaseMessage


@dataclass
class ContextCompilerCache:
    stable_prefix_key: str = ""
    stable_system_content: str = ""
    stable_system_message: SystemMessage | None = None

    row_messages: dict[int, RowMessageCacheEntry] = field(default_factory=dict)
```

`stable_system_message` 只是本地分配优化；正确性由 `stable_system_content` 和输入 key 决定。

### 3.2 Raw History 增量 hydrate

当前：每轮 `messages_from_rows(session_msgs)` 重新构造所有消息对象。

目标：按 `MessageRow` fingerprint 缓存 hydrate 结果。

```python
def row_fingerprint(row: MessageRow) -> str:
    return stable_hash({
        "role": row.role,
        "content": row.content,
        "content_format": row.content_format,
        "tool_calls": row.tool_calls or [],
        "tool_call_id": row.tool_call_id or "",
    })


def messages_from_rows_incremental(
    rows: list[MessageRow],
    cache: dict[int, RowMessageCacheEntry],
) -> tuple[list[BaseMessage], dict[int, RowMessageCacheEntry]]:
    messages: list[BaseMessage] = []
    next_cache: dict[int, RowMessageCacheEntry] = {}

    for row in rows:
        msg_id = row.id
        fingerprint = row_fingerprint(row)
        cached = cache.get(msg_id) if msg_id is not None else None
        if cached is not None and cached.fingerprint == fingerprint:
            message = cached.message
        else:
            message = build_message_from_row(row)
        messages.append(message)
        if msg_id is not None:
            next_cache[msg_id] = RowMessageCacheEntry(fingerprint, message)

    return messages, next_cache
```

说明：

- 缓存 key 不能只看 `row.id`。如果同 id 的 row 内容、格式或 tool metadata 改变，必须重建。
- 编译器只接收 raw semantic messages；这些 messages 不包含 `SystemMessage`，也不包含之前 prepend 的 turn overlay。
- compaction 删除旧 rows 后，下一次 `next_cache` 自然只保留 live rows；cancel 回滚和 session 切换需要显式清空或重建 cache。

### 3.3 Stable Prefix 增量 render

把 system prompt 的稳定部分从 `RuntimeContextBuilder.build()` 中拆出来，按输入 key 复用 rendered content。

```python
@dataclass(frozen=True)
class StablePrefixInput:
    base_system_prompt: str
    role_prompt: str
    mode_prompt: str
    tool_contract: str
    workspace: str
    platform_info: str
    instructions: tuple[str, ...]
    session_date: str
    summary: str


def build_stable_system(
    inp: StablePrefixInput,
    cache: ContextCompilerCache,
) -> tuple[str, ContextCompilerCache]:
    key = stable_hash(inp)
    if cache.stable_prefix_key == key and cache.stable_system_content:
        return cache.stable_system_content, cache

    sections = build_stable_sections(inp)
    content = render_sections(sections)
    cache.stable_prefix_key = key
    cache.stable_system_content = content
    cache.stable_system_message = SystemMessage(content=content)
    return content, cache
```

Stable Prefix 的变化来源：

- `role_prompt`: agent 切换。
- `mode_prompt`: plan/auto/goal 模式切换。
- `tool_contract`: agent/tool contract 变化。
- `workspace/platform_info`: workspace 切换。
- `instructions`: AGENTS.md / CLAUDE.md 内容变化。
- `session_date`: 新 session。
- `summary`: compaction summary 更新。

### 3.4 Turn Overlay 每轮迁移到最新用户消息

Turn Overlay 每轮重新生成，再 prepend 到最新 raw `HumanMessage`。它不进入 history cache，不进入 DB。

```python
def compile_messages(
    raw_messages: list[BaseMessage],
    *,
    system_content: str,
    turn_overlay: str,
    cached_system_message: SystemMessage | None = None,
) -> list[BaseMessage]:
    semantic_messages = [m for m in raw_messages if not isinstance(m, SystemMessage)]
    current_user_index = last_user_index(semantic_messages)

    prefix = (
        cached_system_message
        if cached_system_message is not None and cached_system_message.content == system_content
        else SystemMessage(content=system_content)
    )

    compiled = list(semantic_messages)
    if turn_overlay:
        if current_user_index is None:
            compiled.append(HumanMessage(content=turn_overlay))
        else:
            compiled[current_user_index] = prepend_turn_overlay(
                compiled[current_user_index],
                turn_overlay,
            )

    return [prefix, *compiled]
```

不做 `task_context == cached_task_context -> return message` 这种优化。即使 overlay 内容相同，也必须对当前最新用户消息生成 compiled copy；否则新一轮用户消息会漏注入 overlay。

### 3.5 Instruction 与 Skill 文件缓存

文件内容是 asset，可以缓存；激活结果不是 asset，必须每轮重新计算。

```python
@dataclass
class FileContentCacheEntry:
    mtime_ns: int
    size: int
    content: str
```

策略：

- `InstructionService.system_paths()` 仍每轮执行，保证新增/删除 AGENTS.md 能被发现。
- `_read_file(path)` 使用实例级 cache，key 为 absolute path，状态为 `(mtime_ns, size)`。
- `SkillRegistry` 可以复用实例或引入同样的 file content cache，避免每轮重读和 parse `SKILL.md`。
- `SkillService.select(...)` 仍每轮执行，因为 active skill set 取决于 current user、agent、intent、mode、scope、turn count。
- `SkillService.render_instruction(skill)` 的 full body 仍放在 `Active Skills` turn overlay 里。

### 3.6 Subagent Raw Parent Context

当前 subagent 从 `_current_messages` 复制 parent context。这个列表来自 compiled LangGraph state，可能包含父 agent 的 turn overlay。

改法：

- 主 agent 在 compile 前保留当前 raw semantic frame，或保存 `raw_user_message_by_id`。
- subagent 构造 parent history 时跳过 `SystemMessage`，并把已编译的 latest user message 替换为 raw latest user message。
- subagent 自己再对 `task_description` 这条最新 user message 应用自己的 Turn Overlay。

目标：

```text
Subagent frame:
  SystemMessage(subagent stable prefix)
  raw parent history without parent overlay
  HumanMessage(subagent turn overlay + task_description)
```

这可以防止父 agent 的 Runtime State / Active Skills / Task State 被子 agent 当作历史事实继承。

---

## 4. 缓存失效策略

### 4.1 显式失效

| 事件 | 失效范围 |
|------|---------|
| 新 session / resume session | 清空 row cache；stable prefix 以新 session/workspace 重新 key |
| clear session | 清空 row cache 和 stable prefix cache |
| compaction 完成 | 更新 summary key；删除 compacted rows 对应 row cache |
| cancel 当前 turn | 删除当前 turn 之后 rows；清理对应 row cache |
| agent 切换 | stable prefix key 变化 |
| mode 切换 | stable prefix key 变化；turn overlay 变化 |
| model/profile/permission 修改 | Runtime State overlay 变化 |
| AGENTS.md 修改 | instruction file cache 失效；stable prefix key 变化 |
| SKILL.md 修改 | skill file cache 失效；Active Skills overlay 变化 |

### 4.2 每轮重算

| 内容 | 原因 |
|------|------|
| latest user message | 每轮新输入 |
| Current DateTime | 时间流逝 |
| Active skill activation set | 取决于最新 user/agent/intent/mode/scope |
| Current Task State | task intent、approval、goal、skill runs 可能由上一轮工具调用更新 |

### 4.3 安全策略

宁可多重建，不可漏更新。任何无法证明输入稳定的内容都应重新编译。

---

## 5. 实现步骤

1. **重构 RuntimeContext 输出模型**
   在 `runtime_context.py` 中拆分 Stable Prefix 与 Turn Overlay。保留 `ContextCompiler`，但让它明确接收 raw messages 并返回 compiled frame。

2. **添加 `ContextCompilerCache`**
   在 `VoidXGraph.__init__` 初始化 cache；session resume/clear/cancel/compaction 时失效对应部分。

3. **实现 `messages_from_rows_incremental`**
   在 `message_rows.py` 中新增 row fingerprint 缓存版本，并替换 `_run_once` 的全量 `messages_from_rows(session_msgs)`。

4. **实现 Stable Prefix cache**
   `RuntimeContextBuilder` 或新的 compiler helper 根据 `StablePrefixInput` 生成 key。key 未变时复用 rendered system content。

5. **保留 Turn Overlay 每轮编译**
   Runtime State、Current DateTime、Active Skills、Current Task State 每轮生成，prepend 到当前最新 raw `HumanMessage`。

6. **缓存 instruction / skill 文件内容**
   `InstructionService._read_file` 使用 `(mtime_ns, size)` 实例级缓存。skill registry 复用实例或加入相同文件 cache，但 skill selection 每轮执行。

7. **修正 subagent parent context**
   subagent 不再直接使用 compiled `_current_messages` 作为 parent history；改为 raw parent semantic history，或在复制时替换 latest user 为 raw content。

8. **添加测试**
   覆盖缓存命中、失效、overlay 不持久化、subagent 不继承父 overlay。

---

## 6. 测试覆盖

| 测试 | 描述 |
|------|------|
| `test_incremental_messages_reuses_cached_rows` | 相同 row fingerprint 复用已 hydrate message |
| `test_incremental_messages_rebuilds_changed_row_same_id` | row id 相同但 content/tool metadata 改变时重建 |
| `test_compaction_drops_removed_row_cache_entries` | compaction 后旧 row cache 不再保留 |
| `test_stable_prefix_reuses_rendered_content` | Stable Prefix key 不变时不重新 render system content |
| `test_stable_prefix_rebuilds_on_summary_change` | Long Summary 更新后 stable prefix 重新 render |
| `test_instruction_file_cache_uses_mtime_ns_and_size` | AGENTS.md 未变不重读，快速修改可检测 |
| `test_skill_body_cache_keeps_activation_dynamic` | SKILL.md body 可缓存，但不同 user/intent 会重新选择 skill |
| `test_turn_overlay_updates_current_datetime_each_compile` | Current DateTime 不被 stable prefix 缓存吞掉 |
| `test_compiled_overlay_not_persisted_to_user_history` | 用户消息持久化内容不包含 Runtime State / Active Skills |
| `test_recompile_does_not_duplicate_turn_overlay` | 同一 raw messages 多次 compile 不叠加 overlay |
| `test_subagent_parent_history_strips_parent_overlay` | subagent parent history 不包含父 agent Runtime State / Active Skills / Task State |
| `test_latest_user_receives_active_skill_body` | full skill body 注入最新 user message 的 Turn Overlay |

---

## 7. 预期收益

| 优化项 | 当前开销 | 优化后 |
|--------|---------|--------|
| 历史消息 hydrate | O(N) 每轮 | O(Delta) 新增/变化 rows |
| Instruction 文件读取 | 每轮磁盘 I/O | 文件状态未变时复用内容 |
| Skill 文件读取和 parse | 每轮 discovery/read/parse | 文件状态未变时复用 skill definition/body |
| Stable Prefix render | 每轮拼接所有 stable sections | key 未变时复用 rendered content |
| Turn Overlay | 每轮构建 | 保持每轮构建，保证语义正确 |

Prompt cache 可能受益于稳定的序列化 prefix，但这不是本设计的正确性依据，也不是 V1 验收标准。
