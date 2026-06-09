# Skill 状态机驱动设计

> **Status: Done**
> **Implementation scope**: Phase A 先做 context 分层、去重和 active body cache；Phase B 做 `load_skills`；Phase C 拆分推进。当前只落地 C1 的 transition 元数据和显式事件纯函数；C2/C3 暂不接入运行时自动推断。

## 问题

当前 skill 指令注入是“每 turn 重新匹配”模式：

1. `_build_context()` 每轮调 `skill_context_for()` -> `SkillService.select()`
2. 匹配结果决定 `skill_instructions`，注入到 task context 的 `Active Skills` section
3. 本轮没匹配到的 skill，指令消失，但 `SkillRunState` 还留在 `TaskRun.skill_runs` 里
4. 同一 bundled workflow skill 可能在相邻 LLM call 反复注入完整 body

结果是：**状态和指令生命周期没有对齐**。需要把“skill 状态摘要”和“skill 完整 body”拆开管理。

## 已确认约束

1. **中间 turn 新激活 skill 只更新状态**
   不新增 synthetic `HumanMessage`。`on_intent` 通过 `ToolResult.metadata.state_patch` 激活 skill run；bundled skill body 已在稳定 Skill Context reference library 中，因此不再通过 `ToolResult.output` 重复注入。

2. **Skill Context Message 是稳定 reference library**
   它是 compile-time overlay，只存在于当前 LLM request，不写 SQLite、不进 transcript、不持久化为用户历史。它包含 enabled bundled skill body；真正的激活信号只来自 Current Task State 的 active workflow skills。

3. **自动 workflow selection 只作用于 `scope=bundled`**
   bundled workflow skills 由 voidx 编排；global/project skills 不自动注入完整 body。

4. **`load_skills` everywhere**
   `load_skills` 是只读工具，应在所有 intent/mode 下可见，用于按 skill name 加载 enabled project/global skill 的完整 body。

5. **Subagent 和主 agent 保持一致**
   子 agent 不再手写一套 `skill_instructions=[...]` 注入逻辑，应复用主 agent 的 skill context 构建策略。

## 目标

- 移除 task context 中的 full skill body，保留轻量状态摘要。
- bundled workflow skill 的完整 body 只通过 Skill Context reference library 出现。
- `on_intent` 只激活/合并 skill run state，不返回 bundled skill body。
- 防止同一 skill 在同一 turn 内重复注入。
- project/global skill 只在 stable system 中暴露描述，完整 body 通过 `load_skills` 显式加载。
- 保持 system stable prefix 可缓存，避免 skill body 变化导致 SystemMessage 失效。

## 消息结构

```
[SystemMessage]                 <- stable sections，有缓存
[Skill Context Message]         <- stable reference library：enabled bundled skill body
[历史消息...]                   <- Human/AI/Tool 交替；旧 skill tool body 会被 compiler strip 成摘要
[Task Context + User]           <- 每轮变动，轻量状态摘要，无 full skill body
```

### 各层职责

| 层 | 类型 | 生命周期 | 内容 |
|----|------|----------|------|
| SystemMessage | SystemMessage | stable prefix | Base System、Role Prompt、Mode Prompt、Tool Contract、Workspace Facts、Project Facts、Session Date、Long Summary、Available Skills |
| Skill Context Message | HumanMessage | 当前 LLM request | enabled bundled skill reference library；inactive body 不表示激活 |
| 历史消息 | Human/AI/Tool | 会话历史 | 用户输入、LLM 回复、工具结果；历史中的 skill tool body 需 strip |
| Task Context | HumanMessage 前缀 | 当前 LLM request | Runtime State、DateTime、Current Task State、skill run 摘要 |

## 两种 skill 注入路径

| 场景 | 注入路径 | 是否持久化 full body | 去重策略 |
|------|----------|----------------------|----------|
| enabled bundled workflow skill body | Skill Context Message | 否 | `name + body_hash` cache |
| turn 执行中新激活 bundled skill | `on_intent` state patch | 不返回 full body | merge skill runs + exclude active names |
| project/global skill | `load_skills` ToolResult -> ToolMessage | 仅工具结果；后续可 strip/压缩 | name 去重 + 输出上限 |

## Bundled Workflow Skills

bundled skill 由 voidx 编排。自动选择只扫描 `skill.meta.scope == "bundled"`。

### 激活规则

- turn 开始时：
  - 从当前 turn 解析出的 intent/agent/mode 做 bundled workflow selection
  - review intent 分两类：普通 review 激活 `requesting-code-review`；含 review feedback/审查意见的文本激活 `receiving-code-review`
  - 合并 `TaskRun.skill_runs` 中仍 relevant 的 active run
  - 过滤重复 name
  - 构建 Current Task State 中的 active 摘要
- turn 执行中：
  - `on_intent` 根据 refined intent 选择 bundled workflow skill
  - 排除本 turn 已 active 的 skill
  - 通过 state patch 合并 `SkillRunState`

### Phase A 的 active 生命周期

Phase A 不做复杂 SATISFIED/BLOCKED 推断。active 状态的生命周期按 turn 管理：

- Skill Context Message 只覆盖当前 LLM request，但内容是稳定 bundled reference library。
- `SkillRunState` 持久化状态摘要，但不意味着 full body 必须永久注入。
- 下一 turn 哪些 skill active，由当轮 bundled workflow selection 和 `TaskRun.skill_runs` 去重结果决定。
- 先不实现 `fix_verified`、依赖阻塞、自动 satisfied 这类 evidence 驱动 transition。

### Skill transition 语义

当前 skill 间 transition 是 **soft constraint**：例如 SKILL.md 中写的 “after brainstorming, follow writing-design-docs, then writing-plans” 由 LLM 根据 active skill body 自行遵循，runtime 不强制推进依赖链。

这是本期有意保留的设计取舍：Phase A/B 先保证 context 分层、body 去重和状态摘要稳定。Phase C1 只会把 transition 写成结构化元数据，并提供 `advance_skill_states()` 纯函数。只有调用方传入显式 `SkillStateEvent` 时，runtime 才推进状态；不会从普通 assistant 文本、任意 bash 成功、review 自由文本或 SKILL.md 自然语言里猜测状态完成。

## Project / Global Skills

project/global 自定义 skill 不自动 full body 注入。

### Available Skills

stable system prompt 增加轻量描述：

```text
## Available Skills
- docs: Write and maintain documentation.
- deployment: Deploy the project.
```

规则：

- 只列 enabled skills。
- 默认列 `scope != "bundled"`。
- 只包含 name + description，不包含 body。
- body 文件变化只影响 `load_skills` 结果；description/frontmatter 变化会影响 stable system cache。

### load_skills 工具

`load_skills` 是 read-only runtime tool：

- 所有 intent/mode 下可见，包括 chat/ambiguous/inspect/design/review/debug/implement。
- 不需要写权限审批。
- 只能按 normalized skill name 读取 enabled skill，不能传 path。
- 默认面向 `scope != "bundled"`；如允许加载 bundled，应显式标注来源。
- 一次最多加载 N 个 skill，输出总字符数有上限。
- 找不到或 disabled 时返回结构化错误，不读取任意文件路径。

## Skill Context Message

### 格式

```text
VOIDX_SKILL_CONTEXT

## Skill: systematic-debugging
Source: bundled
Body-Hash: <sha256:16>

<skill body>

## Skill: verification-before-completion
Source: bundled
Body-Hash: <sha256:16>

<skill body>
```

### 缓存

```python
@dataclass
class ContextCompilerCache:
    stable_prefix_key: str = ""
    stable_system_content: str = ""
    stable_system_message: SystemMessage | None = None
    skill_context_key: str = ""
    skill_context_content: str = ""
    skill_context_message: HumanMessage | None = None
    row_messages: dict[int, RowMessageCacheEntry] = field(default_factory=dict)
```

`skill_context_key` 由 sorted active bundled skill 的 `name + body_hash` 组成。key 不变时复用 `skill_context_message`。

## ToolMessage Skill Context

`on_intent` 工具结果使用稳定 marker 包裹 full body：

```text
VOIDX_SKILL_TOOL_CONTEXT
Scope: current-turn

## Skill: systematic-debugging
Source: bundled
Body-Hash: <sha256:16>

<skill body>
```

规则：

- 当前 turn 内保留 full body，供后续 LLM call 使用。
- 下一 turn 编译历史时，compiler 对旧 `ToolMessage` 中的 `VOIDX_SKILL_TOOL_CONTEXT` block 做 stripping。
- stripping 后保留摘要，例如：

```text
VOIDX_SKILL_TOOL_CONTEXT_STRIPPED
- systematic-debugging sha256=<hash> source=bundled
```

这避免旧 tool result 和新 Skill Context Message 同时携带同一份 full body。

## compile_messages 改动

```python
def compile_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
    semantic_messages = raw_semantic_messages(messages)
    semantic_messages = strip_historical_skill_tool_context(semantic_messages)
    current_user_index = _last_user_index(semantic_messages)

    system_content = self.context.render_system()
    prefix = cached_or_new_system_message(system_content)

    task_context = self.context.render_task_context()
    if task_context:
        if current_user_index is None:
            semantic_messages.append(HumanMessage(content=task_context))
        else:
            semantic_messages[current_user_index] = _prepend_task_context(
                semantic_messages[current_user_index],
                task_context,
            )

    result = [prefix]
    if self.context.skill_context_message is not None:
        result.append(self.context.skill_context_message)
    result.extend(semantic_messages)
    return result
```

## Task Context 改动

移除 `Active Skills` full body section，只保留轻量状态摘要：

```python
def _build_task_sections(self) -> list[ContextSection]:
    task_sections = [
        ContextSection(name="Runtime State", content=_render_envelope(envelope)),
        ContextSection(name="Current DateTime", content=self.current_datetime),
        ContextSection(
            name="Current Task State",
            content=self._current_task_state(),
        ),
    ]
    return task_sections
```

`Current Task State` 中保留：

```text
- Active workflow skills: systematic-debugging (debug intent)
- Skill run state: systematic-debugging=active phase=debug source=workflow
```

## 实现阶段

### Phase A: Context 分层和去重

- `SkillRunState` 增加 `skill_body`、`body_hash`。
- `SkillService.select()` 增加 `exclude_names` 和 `scopes`/`bundled_only` 过滤。
- `InstructionService.skill_context_for()` 自动选择 active bundled workflow skills，同时返回 enabled bundled Skill Context reference library。
- `RuntimeContextBuilder` 支持 Skill Context Message。
- `ContextCompilerCache` 缓存 skill context message。
- `_build_task_sections()` 移除 `Active Skills` full body。
- `on_intent` ToolResult 不返回 bundled skill body，只返回 state patch 和 active run 摘要。
- compiler strip 历史 ToolMessage 中的旧 skill body。
- subagent 复用同一套 skill context builder。

### Phase B: load_skills

- 新增 `tools/load_skills.py`。
- 注册到 tool registry，并让所有 intent/mode 可见。
- system stable section 增加 `Available Skills`。
- 限制读取范围、数量、总输出长度。
- 覆盖 disabled/missing skill 的结构化错误。

### Phase C: 状态推进

Phase C 拆成 3 个小阶段，不一次性启用完整硬状态机。

#### Phase C1: transition 元数据 + 显式 evidence 纯函数

C1 可以直接落地，范围保持保守：

- `SkillRunState` 增加 `transition_to: list[str]`。
- `skills/policy.py` 定义 bundled workflow transition metadata。
- 新增 `SkillStateEvent` / `SkillStateEventKind`。
- 新增纯函数 `advance_skill_states(runs, events, turn_count)`，不直接读写 DB、不调用工具、不访问 UI。
- 只处理结构化显式事件，不解析 LLM 自由文本。
- explicit `satisfied` event 只可以把 ACTIVE skill 标记为 SATISFIED，并激活 `transition_to` 后继 skill；PENDING/BLOCKED/SKIPPED 不会被 satisfied event 直接推进。
- explicit `blocked` / `skipped` event 只改变当前 skill，不触发后继。
- explicit `unblocked` event 可以把 BLOCKED skill 恢复为 ACTIVE，不自动满足它。
- 当 event 指向尚不存在的 skill run 时，runtime 按 event kind 建立初始状态：`blocked` -> BLOCKED，`skipped` -> SKIPPED，`satisfied` -> PENDING，`unblocked` 不创建 run。
- existing active/satisfied successor 不重复激活。
- Task Context 通过 `SkillRunState.state_summary()` 展示 `next=...` transition hint。

C1 的状态图：

```text
PENDING -> ACTIVE -> SATISFIED
                  -> BLOCKED -> ACTIVE
                  -> SKIPPED
```

#### Phase C2: evidence 来源接入（暂不做）

C2 再把高置信、结构化来源接入 `advance_skill_states()`：

- tool result metadata 明确声明某个 skill satisfied/blocked；
- `plan_checkpoint` / `clarify` / review agent 等结构化事件；
- verification/test runner 的显式成功或失败事件。
- task/session 持久化路径调用 `advance_skill_states()`，并把结果合并回 `TaskRun.skill_runs`。

#### Phase C3: 有限自动 transition（暂不做）

C3 才启用有限自动 transition：

- 只对明确、低风险的规则自动生成 evidence；
- 不从普通 assistant 文本猜测 skill 完成；
- 不根据任意 bash 成功自动满足 `verification-before-completion`；
- 不把 review 结果自由文本自动解析成 receiving feedback；
- 不把 SKILL.md 中所有自然语言 transition 全量硬编码执行。
- 不做 tool gating 或强制工具顺序。
- 不做跨 turn 的复杂 dependency graph 调度。

Phase A/B 不实现 `fix_verified`、依赖阻塞、自动 satisfied 推断。C1 也不实现这些，只提供状态模型和纯函数。

## 改动文件清单

| 文件 | Phase | 改动 |
|------|-------|------|
| `skills/runtime.py` | A | `SkillRunState` 增加 `skill_body`、`body_hash` |
| `skills/service.py` | A | `select()` 增加 `exclude_names` 和 scope 过滤 |
| `llm/instruction.py` | A | `skill_context_for()` 返回 stable bundled reference library，并动态计算 active 摘要 |
| `agent/runtime_context.py` | A | 新增 Skill Context Message 层、缓存、历史 ToolMessage stripping；移除 `Active Skills` full body |
| `agent/graph/core.py` | A | `_prepare_with_stream()` 使用新 skill context；`on_intent` 合并去重 |
| `agent/graph/subagent.py` | A | 复用主 agent 的 skill context 策略 |
| `tools/on_intent.py` | A | ToolResult 输出 intent/state 摘要，不输出 bundled skill body |
| `tools/load_skills.py` | B | 新增 read-only `LoadSkillsTool` |
| `tools/registry.py` | B | 注册 `load_skills` |
| `skills/policy.py` | A/C1 | A 阶段维护 intent/role 激活策略；C1 新增 transition metadata |
| `skills/runtime.py` | C1 | 新增 `SkillStateEvent` 和 `advance_skill_states()` 纯函数 |

## 不做的事

- Phase A 不把 bundled skill full body 放进 SystemMessage。
- Phase A 不把中间 turn 新激活 skill 改成 synthetic `HumanMessage`。
- Phase A 不做复杂 SATISFIED/BLOCKED 推断。
- Phase A 不改 SKILL.md frontmatter 格式。
- Phase A 不做 skill 间 DAG 依赖。
- Phase C1 不解析 LLM 自由文本。
- Phase C1 不接入 tool result 自动 evidence。
- Phase C1 不启用复杂依赖阻塞、自动 satisfied 或 fix_verified 推断。
- Phase C1 不把 `advance_skill_states()` 接到主 run loop、tool execution 或 DB 写入路径。
- Phase C1 不强制执行 `transition_to`；只有显式 `satisfied` event 触发后继激活。

## 测试计划

### Phase A

- `test_skill_context_message_injected_after_system_before_history`
- `test_active_skills_section_no_longer_contains_full_body`
- `test_bundled_workflow_selection_excludes_user_scoped_skills`
- `test_skill_context_cache_reuses_message_when_body_hash_unchanged`
- `test_skill_context_cache_rebuilds_when_body_hash_changes`
- `test_on_intent_no_longer_injects_skill_body_in_tool_result`
- `test_historical_skill_tool_context_is_stripped_next_turn`
- `test_skill_service_activates_requesting_code_review_for_review_intent`
- `test_skill_service_activates_receiving_code_review_for_feedback`
- `test_skill_transitions_are_soft_constraints_documented`
- `test_same_skill_not_reinjected_twice_in_same_turn`
- `test_skill_context_overlay_not_persisted_to_user_history`
- `test_subagent_uses_same_skill_context_strategy_as_orchestrator`

### Phase B

- `test_available_skills_lists_enabled_user_project_global_skills_only`
- `test_load_skills_visible_for_chat_inspect_and_implement`
- `test_load_skills_loads_enabled_skill_by_name`
- `test_load_skills_rejects_path_input`
- `test_load_skills_respects_disabled_skills`
- `test_load_skills_enforces_count_and_size_limits`

### Phase C

- `test_skill_run_state_from_match_includes_transition_targets`
- `test_skill_state_summary_includes_transition_hint`
- `test_advance_skill_states_marks_satisfied_from_evidence`
- `test_advance_skill_states_does_not_mark_pending_satisfied`
- `test_advance_skill_states_initializes_missing_run_from_event_kind`
- `test_advance_skill_states_activates_transition_target`
- `test_advance_skill_states_does_not_advance_without_evidence`
- `test_advance_skill_states_does_not_duplicate_existing_successor`
- `test_blocked_or_skipped_skill_does_not_trigger_successor`
- `test_blocked_skill_can_reactivate_when_condition_clears`

## 风险

1. **历史 ToolMessage stripping 误删普通工具输出**
   缓解：只处理 `VOIDX_SKILL_TOOL_CONTEXT` marker 包裹的 block，不做模糊文本匹配。

2. **Skill Context Message 仍可能造成上下文膨胀**
   缓解：Phase A 只放 bundled active skill，且按 body hash 去重/cache。

3. **用户 skill 需要额外 tool call 才能拿到 body**
   这是有意设计，避免所有 project/global skill 自动污染上下文。

4. **Subagent 与主 agent 行为分叉**
   缓解：subagent 不自行拼 `skill_instructions`，复用同一套 builder/API。
