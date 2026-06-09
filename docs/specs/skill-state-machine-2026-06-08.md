# Skill 状态机驱动设计

> **Status: Draft**

## 问题

当前 skill 指令注入是"每 turn 重新匹配"模式：

1. `_build_context()` 每轮调 `skill_context_for()` → `SkillService.select()`
2. 匹配结果决定 `skill_instructions`，注入到 task context（`Active Skills` section）
3. 本轮没匹配到的 skill，指令消失，但 `SkillRunState` 还留在 `TaskRun.skill_runs` 里
4. 结果：**状态在，指令丢了**；或者同一 skill 反复匹配导致指令重复注入

## 设计：固定技能区域 + 消息内技能

### 三层消息结构

```
[SystemMessage]              ← stable sections，不变，有缓存
[Skill Context Message]      ← 固定技能区域：本轮开始时已激活的 skill body，后续不变
[历史消息...]                ← Human/AI 交替（含中间 turn 新激活的 skill 指令）
[Task Context + User]        ← 每轮变动，轻量（只有状态摘要，无 skill body）
```

### 两种 skill 注入方式

| 类型 | 注入位置 | 变动频率 | 重复加载 |
|------|----------|----------|----------|
| 本轮开始时已激活的 skill | Skill Context Message（固定技能区域） | 不变 | 否 |
| 中间 turn 新激活的 skill | HumanMessage 区域（作为对话消息的一部分） | 激活时注入一次 | 是（作为历史消息保留） |

**固定技能区域**：每个 turn 开始时，从 `TaskRun.skill_runs` 取所有 ACTIVE skill 的 body，构建 Skill Context Message。这个消息在 turn 内不变，不重复加载。下一轮开始时重新构建（可能有 skill 退出或新激活）。

**消息内技能**：在 turn 执行过程中，如果 `on_intent` 或触发词匹配到新 skill，将完整 body 作为一条 HumanMessage 注入到对话历史中。这条消息作为对话的一部分永久保留，后续 turn 不需要重新注入（已经在历史里了）。如果同一 skill 在不同 turn 被再次触发，会重复注入——这是允许的，因为它是新的对话上下文。

### 各层职责

| 层 | 类型 | 变动频率 | 内容 |
|----|------|----------|------|
| SystemMessage | SystemMessage | 几乎不变 | Base System、Role Prompt、Mode Prompt、Tool Contract、Workspace Facts、Project Facts、Session Date、Long Summary、用户 skill 描述 |
| Skill Context Message | HumanMessage | 每轮开始时构建，turn 内不变 | 本轮开始时已 ACTIVE 的内置 skill 完整 body |
| 历史消息 | Human/AI 交替 | 每轮增长 | 用户输入 + LLM 回复 + 中间 turn 新激活的 skill 指令 |
| Task Context | HumanMessage 前缀 | 每轮变动 | Runtime State、DateTime、Current Task State（含 skill 状态摘要行） |

### 关键变化

1. **Active Skills section 从 task sections 移除**：task sections 只保留轻量状态摘要
2. **Skill Context Message 只放本轮开始时已激活的 skill**：构建后 turn 内不变，不重复加载
3. **中间 turn 新激活的 skill 注入到对话历史**：作为 HumanMessage 永久保留，可重复加载

## 两类 skill，两种策略

### 1. 内置工作流 skill（bundled）

voidx 自己编排，匹配只在激活时做一次。

**激活时机与注入方式**：

| 激活时机 | 注入方式 |
|----------|----------|
| turn 开始时已 ACTIVE | Skill Context Message（固定技能区域） |
| turn 执行中新激活 | HumanMessage（对话历史，永久保留） |

**匹配逻辑**：
- turn 开始时：从 `TaskRun.skill_runs` 恢复已有 ACTIVE skill → 构建 Skill Context Message
- turn 执行中：`select()` 匹配新 skill（排除已 ACTIVE 的）→ 注入到对话历史
- 退出：transition 规则推进到 SATISFIED/BLOCKED → 下一轮 Skill Context Message 不再包含

### 2. 用户自定义 skill（global / project）

voidx 不自动编排，只把描述放到 system prompt，LLM 自己决定是否加载。

**注入位置**：
- **system prompt**（stable sections）：`Available Skills` section，列出 name + description
- **load_skills 工具**：LLM 调用时读取指定 skill 的完整 body，返回给 LLM

## Skill Context Message 实现

### 消息格式

```
VOIDX_SKILL_CONTEXT

## Skill: systematic-debugging
<skill body>

## Skill: verification-before-completion
<skill body>
```

### 缓存机制

```python
@dataclass
class ContextCompilerCache:
    stable_prefix_key: str = ""
    stable_system_content: str | None = None
    stable_system_message: SystemMessage | None = None
    skill_context_key: str = ""          # 新增
    skill_context_content: str | None = None  # 新增
    skill_context_message: HumanMessage | None = None  # 新增
```

Skill Context Message 的 key 由所有 ACTIVE skill 的 name + body hash 组成。key 不变时复用缓存消息。

### compile_messages 改动

```python
def compile_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
    semantic_messages = raw_semantic_messages(messages)
    current_user_index = _last_user_index(semantic_messages)

    # SystemMessage（不变）
    system_content = self.context.render_system()
    cached_system = self.context.system_message
    prefix = (
        cached_system
        if cached_system is not None and cached_system.content == system_content
        else SystemMessage(content=system_content)
    )

    # Skill Context Message（固定技能区域，turn 内不变）
    skill_msg = self.context.skill_context_message

    # Task Context（每轮变动，轻量）
    task_context = self.context.render_task_context()
    if task_context:
        if current_user_index is None:
            semantic_messages.append(HumanMessage(content=task_context))
        else:
            current = semantic_messages[current_user_index]
            semantic_messages[current_user_index] = _prepend_task_context(current, task_context)

    result = [prefix]
    if skill_msg is not None:
        result.append(skill_msg)
    result.extend(semantic_messages)
    return result
```

### _build_task_sections 改动

移除 `Active Skills` section，只保留轻量内容：

```python
def _build_task_sections(self) -> list[ContextSection]:
    task_sections = [
        ContextSection(name="Runtime State", content=_render_envelope(envelope)),
        ContextSection(name="Current DateTime", content=self.current_datetime),
    ]
    # 不再有 Active Skills section
    task_sections.append(ContextSection(
        name="Current Task State",
        content=self._current_task_state(),  # 包含 Skill run state 摘要行
    ))
    return task_sections
```

## 中间 turn 新激活 skill 的注入

当 turn 执行过程中（如 `on_intent` 工具调用后）匹配到新 skill：

```python
# 在 on_intent resolver 或 tool_execution 中
new_skill = skill_service.select(user_text, exclude_names=active_names)
if new_skill:
    for match in new_skill:
        run = SkillRunState.from_match(match, ...)
        skill = skill_service.get(match.name)
        if skill:
            run.skill_body = skill.body
        # 注入到对话历史
        instruction = skill_service.render_instruction(skill)
        messages.append(HumanMessage(content=instruction))
        messages.append(AIMessage(content=f"Following skill: {match.name}."))
```

这些消息作为对话的一部分永久保留，不需要在后续 turn 重新注入。如果同一 skill 在不同 turn 被再次触发，会重复注入——这是允许的，因为它是新的对话上下文。

## 状态机

```
PENDING → ACTIVE → SATISFIED
                 ↘ BLOCKED → ACTIVE (re-activate)
                 ↘ SKIPPED
```

| 状态 | 含义 | 固定技能区域 | Task Context 摘要 |
|------|------|-------------|-------------------|
| ACTIVE | 正在执行 | 包含完整 body | `skill=active` |
| SATISFIED | 目标达成 | 移除 | `skill=satisfied` |
| BLOCKED | 被阻塞 | 移除 | `skill=blocked` |
| SKIPPED | 跳过 | 移除 | `skill=skipped` |

### 状态转换触发（仅内置 skill）

| 从 | 到 | 触发条件 |
|----|----|----------|
| - | ACTIVE | `on_intent` 匹配 / 显式引用 / 触发词命中 |
| ACTIVE | SATISFIED | transition 规则满足 |
| ACTIVE | BLOCKED | 依赖条件不满足 |
| BLOCKED | ACTIVE | 阻塞条件解除 |
| ACTIVE | SKIPPED | 用户显式跳过 |

## 核心改动

### 1. 匹配与注入解耦

**当前**：每 turn `select()` → `matches` → `skill_instructions`

**改为**：

```python
# _build_context() 中的新逻辑

# 1. 恢复已有 skill 状态
existing_runs = _restored_skill_runs(self._task_run)
active_names = {r.name for r in existing_runs if r.status == SkillRunStatus.ACTIVE}

# 2. 只匹配新 skill（排除已激活的）
new_matches = skill_service.select(
    user_text,
    agent=agent,
    task_intent=task_intent,
    interaction_mode=interaction_mode,
    exclude_names=active_names,
)

# 3. 新匹配 → 创建 SkillRunState(ACTIVE)，缓存 skill_body
new_runs = []
for match in new_matches:
    run = SkillRunState.from_match(match, ...)
    skill = skill_service.get(match.name)
    if skill:
        run.skill_body = skill.body
    new_runs.append(run)

# 4. 合并
all_runs = _merge_skill_runs(existing_runs, new_runs)

# 5. 状态推进
advance_skill_states(all_runs, state)

# 6. 构建 Skill Context Message（固定技能区域，只含本轮开始时已 ACTIVE 的）
skill_context_key, skill_context_msg = _build_skill_context(existing_runs)

# 7. 构建 task sections（轻量，不含 skill body）
skill_instructions = []  # 不再传给 task sections
```

### 2. SkillService.select() 增加 `exclude_names`

```python
def select(self, user_text, *, exclude_names=None, limit=5):
    # ... 现有逻辑 ...
    if exclude_names:
        matches = [m for m in matches if normalize_skill_name(m.name) not in exclude_names]
    return matches[:limit]
```

### 3. SkillRunState 增加 `skill_body`

```python
class SkillRunState(BaseModel):
    name: str
    status: SkillRunStatus = SkillRunStatus.ACTIVE
    # ... 现有字段 ...
    skill_body: str = ""  # 新增：激活时缓存
```

### 4. 用户自定义 skill 描述注入 system prompt

```python
# _build_stable_sections() 中新增
user_skills = [s for s in enabled_skills if s.meta.scope != "bundled"]
if user_skills:
    sections.append(ContextSection(
        name="Available Skills",
        content="\n".join(
            f"- {skill.name}: {skill.meta.description}"
            for skill in user_skills
        ),
    ))
```

### 5. load_skills 工具

```python
class LoadSkillsTool(BaseTool):
    id = "load_skills"
    description = (
        "Load the full instructions of one or more user-installed skills by name. "
        "Use this when you decide to follow a skill listed in the Available Skills section."
    )
```

### 6. 状态推进：transition 规则

首期硬编码在 `policy.py`：

```python
SKILL_TRANSITIONS = {
    "verification-before-completion": {
        "satisfied": lambda runs, state: any(
            r.name == "systematic-debugging" and r.status == SkillRunStatus.SATISFIED
            for r in runs
        ),
        "blocked": lambda runs, state: any(
            r.name == "systematic-debugging" and r.status == SkillRunStatus.ACTIVE
            for r in runs
        ),
    },
    "systematic-debugging": {
        "satisfied": lambda runs, state: any(
            e.kind == "fix_verified" and e.ok
            for r in runs if r.name == "systematic-debugging"
            for e in r.evidence
        ),
    },
}
```

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `skills/runtime.py` | `SkillRunState` 增加 `skill_body` 字段 |
| `skills/service.py` | `select()` 增加 `exclude_names` 参数 |
| `skills/policy.py` | 新增 `SKILL_TRANSITIONS` 和 `advance_skill_states()` |
| `tools/load_skills.py` | 新增 `LoadSkillsTool` |
| `tools/registry.py` | 注册 `load_skills` 工具 |
| `agent/runtime_context.py` | 新增 Skill Context Message 层；`_build_task_sections()` 移除 Active Skills section；`compile_messages()` 插入 skill context；缓存扩展 |
| `agent/graph/core.py` | `_build_context()` 改为状态驱动，构建 skill context；中间 turn 新激活 skill 注入对话历史 |
| `llm/instruction.py` | `skill_context_for()` 适配新流程 |

## 不做的事

- 不把内置 skill 指令移到 system prompt——会导致缓存失效
- 不做 skill 间的 DAG 依赖——首期用简单的阻塞/满足规则
- 不做 skill_done 工具——首期用 transition 规则自动推进
- 不改 SKILL.md 格式——首期 transition 规则硬编码
- 不改 `_strip_turn_overlay` 逻辑——task context 的清空机制已正确

## 风险

1. **Skill Context Message 变动导致缓存失效**：skill 激活/退出时，Skill Context Message 内容变化，但只影响这一条消息，不影响 SystemMessage 缓存。
2. **中间 turn 新激活 skill 的重复加载**：同一 skill 在不同 turn 被再次触发时会重复注入到对话历史。这是允许的，但可能导致历史膨胀。缓解：compaction 会压缩历史。
3. **transition 规则硬编码**：首期不够灵活，后续迭代支持 SKILL.md 声明式。
4. **load_skills 工具增加一轮 tool call**：LLM 需要额外调用才能拿到用户 skill body。但这是有意为之——用户 skill 不应自动注入。
