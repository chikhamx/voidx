# Context Incremental Build — 增量构建设计文档

Date: 2026-06-06

## 1. 背景与动机

### 1.1 当前流程

每轮对话，context 从零重建：

```
Turn N:
  1. session_msgs = list(self._session_msg_cache)     # 拷贝 MessageRow 列表
  2. msgs = messages_from_rows(session_msgs)           # 逐行 new HumanMessage/AIMessage/ToolMessage
  3. msgs.append(turn_msg)                             # 追加当前用户消息
  4. _maybe_compact(msgs, ...)                         # 可能压缩
  5. graph.ainvoke(initial, ...)                       # 进入 LangGraph
     5a. _prepare_with_stream:
         - await self._instruction.system()             # 每轮重读 AGENTS.md
         - await self._instruction.skill_context_for()  # 每轮重选 skill
         - RuntimeContextBuilder(...).build()           # 重建所有 sections
         - context.apply_to_messages(state["messages"]) # 剥离旧 SystemMessage + 新建 + prepend task context
     5b. _call_llm → stream_llm → API call
     5c. _execute_tools → tool calls
     5d. 循环 5b-5c 直到无工具调用
  6. _compaction.prune(final["messages"])               # 截断旧工具输出
  7. 持久化新消息到 DB
```

### 1.2 问题

| 问题 | 影响 |
|------|------|
| **每轮全量重建消息列表** | `messages_from_rows` 对每条 MessageRow 都 `new HumanMessage/AIMessage(...)`，100 条消息 = 100 次对象构造 + `parse_structured_content` 可能 JSON 解析 |
| **每轮重读 AGENTS.md** | `InstructionService.system()` 每轮都 `Path.read_text()` 读磁盘，文件没变也重读 |
| **每轮重建 RuntimeContext** | `RuntimeContextBuilder.build()` 每轮重新拼接所有 section 字符串，即使 Base System / Role Prompt / Tool Contract / Workspace Facts / Project Facts 完全没变 |
| **每轮重建 SystemMessage** | `compile_messages` 每轮 `new SystemMessage(content=render_system())`，即使内容相同也是新对象 |
| **每轮重建 task context** | `_prepend_task_context` 用 `model_copy` 创建新 HumanMessage，即使 task state 没变也重建 |
| **prompt cache 命中率低** | SystemMessage 每轮是新对象 → API 侧 cache key 变化 → 即使内容相同也 cache miss |

### 1.3 目标

1. **减少每轮重复计算**：稳定内容只构建一次，后续轮次复用。
2. **提高 prompt cache 命中率**：SystemMessage 对象稳定 → API 侧 cache key 稳定 → 命中率提升。
3. **减少消息对象重建**：历史消息不每轮重新构造，只在变化时更新。
4. **保持语义正确**：动态内容（Current DateTime、Runtime State、Task State）仍每轮更新，但不影响稳定部分。

---

## 2. 稳定性分析

### 2.1 Context Sections 稳定性分类

| Section | 稳定性 | 变化触发条件 |
|---------|--------|-------------|
| Base System | 🟢 永不变 | 代码升级 |
| Role Prompt | 🟢 永不变 | 切换 agent |
| Mode Prompt | 🟡 偶尔变 | 切换 plan mode |
| Tool Contract | 🟢 永不变 | 代码升级 |
| Workspace Facts | 🟢 永不变 | 切换 workspace |
| Project Facts | 🟡 偶尔变 | AGENTS.md 文件修改 |
| Session Date | 🟢 永不变 | 新 session |
| Long Summary | 🔴 每次压缩后变 | compaction 发生 |

| Task Section | 稳定性 | 变化触发条件 |
|-------------|--------|-------------|
| Runtime State | 🟡 偶尔变 | 切换 mode/agent/permission |
| Current DateTime | 🔴 每轮变 | 时间流逝 |
| Active Skills | 🟡 偶尔变 | skill 匹配变化 |
| Current Task State | 🔴 每轮变 | intent/approval/goal 变化 |

### 2.2 消息对象稳定性

| 消息类型 | 稳定性 | 说明 |
|---------|--------|------|
| 历史 HumanMessage | 🟢 稳定 | DB 读取后不修改（task context 只 prepend 最后一条） |
| 历史 AIMessage | 🟢 稳定 | 原样保留 |
| 历史 ToolMessage | 🟢 稳定 | prune 可能截断内容，但同一 session 内只截断一次 |
| 最新 HumanMessage | 🔴 每轮变 | 新的用户输入 + task context prepend |
| SystemMessage | 🟡 条件稳定 | 内容不变时对象应稳定（当前每轮新建） |

---

## 3. 设计方案

### 3.1 核心思路：Stable Context Cache

在 `VoidXGraph` 上维护一个 `_stable_context` 缓存，记录上一次构建的 context 状态。每轮构建时，对比哪些 section 没变，复用上一次的结果。

```python
@dataclass
class StableContextCache:
    """缓存上一次构建的 context，用于增量更新。"""
    # 输入指纹 — 用于判断是否需要重建
    system_prompt_hash: str = ""        # Base System + Role + Mode + Tool Contract
    instructions_hash: str = ""         # AGENTS.md 内容 hash
    workspace_hash: str = ""            # workspace path + platform
    session_date: str = ""
    summary_hash: str = ""

    # 构建产物 — 可复用
    system_content: str = ""            # render_system() 的完整输出
    task_sections_content: str = ""     # render_task_context() 的完整输出（不含动态部分）

    # 消息缓存 — 避免每轮重新构造
    message_cache: dict[int, BaseMessage] = field(default_factory=dict)  # row.id → Message
    last_row_count: int = 0
```

### 3.2 增量消息构建

当前：每轮 `messages_from_rows(session_msgs)` 重新构造所有消息对象。

优化：缓存已构造的消息对象，只构造新增的。

```python
def messages_from_rows_incremental(
    rows: list[MessageRow],
    cache: dict[int, BaseMessage],
    last_count: int,
) -> tuple[list[BaseMessage], dict[int, BaseMessage], int]:
    """增量构建消息列表，复用缓存中已有的消息对象。"""
    messages: list[BaseMessage] = []
    new_cache: dict[int, BaseMessage] = {}

    for row in rows:
        msg_id = row.id
        if msg_id is not None and msg_id in cache:
            # 复用缓存的消息对象
            messages.append(cache[msg_id])
            new_cache[msg_id] = cache[msg_id]
        else:
            # 新消息，需要构造
            msg = _build_message(row)
            messages.append(msg)
            if msg_id is not None:
                new_cache[msg_id] = msg

    return messages, new_cache, len(rows)
```

**注意**：compaction 会删除旧消息并修改 `_session_msg_cache`，此时需要清除对应缓存条目。在 `_persist_compaction` 中已有 `self._session_msg_cache = [r for r in cache if ...]`，同步清理 `message_cache` 即可。

### 3.3 增量 System Prompt 构建

当前：每轮 `RuntimeContextBuilder.build()` 重新拼接所有 section。

优化：计算 section 指纹，只重建变化的部分。

```python
class RuntimeContextBuilder:
    def build_incremental(
        self,
        cache: StableContextCache,
    ) -> tuple[RuntimeContext, StableContextCache]:
        # 计算稳定部分的指纹
        system_hash = _hash(self.base_system_prompt + self.role_prompt + self.tool_contract)
        instructions_hash = _hash("\n".join(self.instructions))
        workspace_hash = _hash(self.workspace + _platform_info())
        summary_hash = _hash(self.summary)

        # 判断是否需要重建 system content
        system_changed = (
            cache.system_prompt_hash != system_hash
            or cache.instructions_hash != instructions_hash
            or cache.workspace_hash != workspace_hash
            or cache.session_date != self.session_date
            or cache.summary_hash != summary_hash
        )

        if system_changed:
            # 重建 system sections
            sections = self._build_sections()
            system_content = _render_sections(sections)
            cache = StableContextCache(
                system_prompt_hash=system_hash,
                instructions_hash=instructions_hash,
                workspace_hash=workspace_hash,
                session_date=self.session_date,
                summary_hash=summary_hash,
                system_content=system_content,
            )
        else:
            # 复用上一次的 system content
            sections = self._build_sections()  # 仍需构建 sections 对象用于 RuntimeContext
            # 但 render_system() 的结果已知，直接使用缓存

        # task sections 始终重建（含动态内容）
        task_sections = self._build_task_sections()

        return RuntimeContext(sections=sections, task_sections=task_sections), cache
```

### 3.4 SystemMessage 对象稳定化

当前：`compile_messages` 每轮 `new SystemMessage(content=...)`，即使内容相同也是新对象。

优化：当 system content 未变时，复用上一次的 SystemMessage 对象。

```python
class ContextCompiler:
    def compile_messages(
        self,
        messages: list[BaseMessage],
        cached_system: SystemMessage | None = None,
    ) -> tuple[list[BaseMessage], SystemMessage | None]:
        semantic_messages = [m for m in messages if not isinstance(m, SystemMessage)]
        current_user_index = _last_user_index(semantic_messages)

        system_content = self.context.render_system()
        if cached_system and cached_system.content == system_content:
            prefix = cached_system  # 复用
        else:
            prefix = SystemMessage(content=system_content)  # 新建

        task_context = self.context.render_task_context()
        if task_context:
            if current_user_index is None:
                semantic_messages.append(HumanMessage(content=task_context))
            else:
                current = semantic_messages[current_user_index]
                semantic_messages[current_user_index] = _prepend_task_context(current, task_context)

        return [prefix, *semantic_messages], prefix
```

**为什么 SystemMessage 对象稳定很重要？**

LLM 提供商（Anthropic、OpenAI）的 prompt cache 机制基于请求内容的 prefix hash。如果 SystemMessage 对象每轮新建，即使内容相同，序列化后的字节可能因 Python dict 排序、浮点精度等原因产生微小差异，导致 cache miss。复用同一对象可以保证序列化结果完全一致。

### 3.5 InstructionService 缓存

当前：`InstructionService.system()` 每轮 `Path.read_text()` 读磁盘。

优化：基于 mtime 缓存文件内容。

```python
class InstructionService:
    _content_cache: dict[str, tuple[float, str]] = {}  # path → (mtime, content)

    async def _read_file(self, path: str) -> str:
        mtime = Path(path).stat().st_mtime
        if path in self._content_cache:
            cached_mtime, cached_content = self._content_cache[path]
            if cached_mtime == mtime:
                return cached_content
        content = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8", errors="replace"))
        self._content_cache[path] = (mtime, content)
        return content
```

### 3.6 Task Context 增量更新

当前：每轮 `_prepend_task_context` 用 `model_copy` 创建新 HumanMessage，即使 task state 没变。

优化：对比 task context 内容，未变时复用上一次的 HumanMessage。

```python
def _prepend_task_context_if_changed(
    message: BaseMessage,
    task_context: str,
    cached_task_context: str | None = None,
) -> tuple[BaseMessage, str]:
    """只在 task context 变化时创建新消息对象。"""
    if task_context == cached_task_context:
        return message, task_context  # 复用
    return _prepend_task_context(message, task_context), task_context
```

---

## 4. 缓存失效策略

### 4.1 显式失效

| 事件 | 失效范围 |
|------|---------|
| compaction 完成 | `summary_hash`、`message_cache` 中被删除的条目 |
| prune 截断工具输出 | 对应 ToolMessage 的缓存条目 |
| 切换 agent | `system_prompt_hash`（role_prompt 变化） |
| 切换 mode | `system_prompt_hash`（mode_prompt 变化） |
| AGENTS.md 修改 | `instructions_hash`（mtime 检测） |
| 新 session | 全部失效 |

### 4.2 隐式失效

| 条件 | 检测方式 |
|------|---------|
| Current DateTime 变化 | 每轮对比，始终重建 |
| Runtime State 变化 | 对比 `render_envelope()` 输出 |
| Task State 变化 | 对比 `_current_task_state()` 输出 |
| Skill 匹配变化 | 对比 skill instructions 内容 |

### 4.3 安全策略

**宁可多重建，不可漏更新。** 当无法确定是否变化时，选择重建而非复用。缓存只是优化手段，正确性优先。

---

## 5. 预期收益

### 5.1 性能

| 优化项 | 当前开销 | 优化后 |
|--------|---------|--------|
| 消息对象构造 | O(N) 每轮 | O(Δ) 只构造新增消息 |
| AGENTS.md 读取 | 每轮磁盘 I/O | mtime 未变时跳过 |
| System prompt 拼接 | 每轮字符串拼接 | 未变时复用 |
| SystemMessage 对象 | 每轮 new | 未变时复用 |
| Task context prepend | 每轮 model_copy | 未变时跳过 |

### 5.2 Prompt Cache 命中率

- SystemMessage 对象稳定 → API 侧 prefix hash 稳定 → cache 命中
- 非压缩轮次，system prompt 完全不变 → 100% cache hit
- 压缩轮次，Long Summary 变化 → 只有 Summary 之后的部分 cache miss

### 5.3 内存

- `message_cache` 持有消息对象引用，避免每轮 GC 压力
- `StableContextCache` 本身很小（几个 hash + 两个字符串）

---

## 6. 实现步骤

1. **添加 `StableContextCache` 数据类** — 在 `runtime_context.py` 中定义。
2. **实现 `messages_from_rows_incremental`** — 在 `message_rows.py` 中，替换 `messages_from_rows` 的调用点。
3. **实现 `InstructionService._read_file` mtime 缓存** — 在 `instruction.py` 中。
4. **实现 `RuntimeContextBuilder.build_incremental`** — 在 `runtime_context.py` 中。
5. **实现 `ContextCompiler.compile_messages` 的 SystemMessage 复用** — 在 `runtime_context.py` 中。
6. **在 `VoidXGraph` 上维护 `_stable_context`** — 在 `core.py` 中初始化，在 `_prepare_with_stream` 中使用。
7. **处理 compaction/prune 的缓存失效** — 在 `compaction.py` 中同步清理。
8. **添加测试** — 验证缓存命中/失效逻辑。

---

## 7. 测试覆盖

| 测试 | 描述 |
|------|------|
| `test_incremental_messages_reuses_cached` | 相同 rows 返回相同消息对象 |
| `test_incremental_messages_builds_new` | 新增 rows 只构造新消息 |
| `test_compaction_clears_message_cache` | 压缩后旧消息缓存被清除 |
| `test_stable_system_prompt_reuses_object` | system content 未变时复用 SystemMessage |
| `test_stable_system_prompt_rebuilds_on_change` | summary 变化时重建 SystemMessage |
| `test_instruction_mtime_cache` | AGENTS.md 未修改时跳过磁盘读取 |
| `test_task_context_reuse_when_unchanged` | task state 未变时复用 HumanMessage |
| `test_cache_invalidation_on_agent_switch` | 切换 agent 时 system cache 失效 |
| `test_cache_invalidation_on_mode_switch` | 切换 mode 时 system cache 失效 |
| `test_prompt_cache_hit_rate_improvement` | 非压缩轮次 SystemMessage 对象稳定 |
