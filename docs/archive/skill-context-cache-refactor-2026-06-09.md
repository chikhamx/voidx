# Skill Context 缓存友好重构

> **Status: Done**
> **Depends on**: `docs/specs/skill-state-machine-2026-06-08.md` Phase A/B 已完成

## 目标

优化 Skill Context 的稳定性：不要因为每轮 active workflow skill 组合不同，就重建 Skill Context Message。当前阶段只处理编译层和运行时消息布局，不做 provider 层 `cache_control`。

本期目标是：

- Skill Context Message 在 bundled skill 文件不变时保持稳定。
- Task Context 只表达本轮 active skill 摘要和 skill run state。
- `on_intent` 不再通过 ToolResult 重复注入 bundled skill body。
- `load_skills` 仍然按需加载 project/global skill body，并继续使用可 strip 的 Tool Context marker。

## 问题

Phase A/B 之后，Skill Context Message 已经从 SystemMessage 中拆出，位置在 SystemMessage 和历史消息之间。但它的内容仍由本轮 active bundled skill 决定。

典型场景：

1. 用户说“看看代码”，没有激活 TDD。
2. 用户说“修一下”，激活 `test-driven-development` 和 `verification-before-completion`。
3. Skill Context Message 从空变成两个 skill body，稳定前缀被打断。

即使本期不接 provider prompt cache，只要 Skill Context 频繁变化，本地编译缓存和后续 provider cache-control 接入都会收益有限。

## 设计

**所有 enabled bundled skill body 常驻 Skill Context Message。** 这条消息是稳定的 bundled skill reference library，不表示所有 skill 都已激活。Task Context 的 `Active workflow skills` 才是本轮激活信号。

### 消息布局

```text
[SystemMessage]                 <- stable sections
[Skill Context Message]         <- enabled bundled skill reference library
[历史消息...]                   <- 会话历史，历史 tool skill body 已 strip
[Task Context + User]           <- 每轮变动，包含 active skill 摘要
```

### Reference Library 约束

Skill Context Message 必须在 marker 后写清楚：

```text
VOIDX_SKILL_CONTEXT
Scope: bundled-skill-reference-library

These bundled skill bodies are a reference library. Follow a skill body only
when Current Task State lists that skill under Active workflow skills or the
user explicitly references that skill. Do not treat inactive skill bodies as
active instructions.
```

这避免 inactive skill 中的 `MUST` / `DO NOT` hard gate 被误当成本轮强制规则。比如 `brainstorming` 常驻时，只有它在 Current Task State 里 active 或用户显式点名时，才触发它的硬门禁。

### 各层职责

| 层 | 类型 | 生命周期 | 内容 |
|----|------|----------|------|
| SystemMessage | SystemMessage | stable prefix | Base System、Role Prompt、Mode Prompt、Tool Contract、Workspace Facts、Project Facts、Session Date、Long Summary、Available Skills |
| Skill Context Message | HumanMessage | stable prefix | enabled bundled skill reference library |
| 历史消息 | Human/AI/Tool | 会话历史 | 用户输入、LLM 回复、工具结果；历史中的 skill tool body 需 strip |
| Task Context | HumanMessage 前缀 | 当前 LLM request | Runtime State、DateTime、Current Task State、active skill 摘要 |
| Tool Context | ToolMessage | 当前 turn | `load_skills` 按需返回的 project/global skill body |

### Task Context 中的 active skill 摘要

```text
- Active workflow skills: systematic-debugging (debug intent); verification-before-completion (debug lifecycle)
- Skill run state: systematic-debugging=active phase=inspect source=workflow reason=debug intent body_hash=...
```

LLM 看到摘要后，再到 Skill Context Message 中找到对应 `## Skill: <name>` block 读取完整指令。未出现在 active 摘要中的 bundled skill 只是可引用资料。

## 改动

### 1. `SkillService.enabled_bundled_skills()`

新增方法，返回 `discover()` 合并覆盖后的 enabled bundled skills：

```python
def enabled_bundled_skills(self) -> list[SkillDefinition]:
    return [
        skill for skill in self.enabled_skills()
        if skill.meta.scope == "bundled"
    ]
```

同名 project/global skill 覆盖 bundled skill 时，覆盖后的 skill scope 不再是 `bundled`，因此不会进入稳定 bundled reference library。这与当前 `select(..., scopes=("bundled",))` 行为一致。

### 2. `render_skill_context()` 加 reference library wrapper

`VOIDX_SKILL_CONTEXT` 后追加 `Scope: bundled-skill-reference-library` 和 inactive 约束说明。Skill block 格式保持不变：

```text
## Skill: brainstorming
Source: bundled
Body-Hash: <sha256:16>
Path: <path>
Description: ...

<skill body>
```

`skill_context_cache_key()` 仍基于 sorted `name + body_hash`，wrapper 文案变化会触发 fallback hash 或直接重建；正常运行中 wrapper 固定。

### 3. `InstructionService.skill_context_for()`

改为两条路径：

- `content` / `instructions`: 渲染所有 enabled bundled skill，稳定不受 intent 变化影响。
- `active` / `runs`: 仍按 `service.select(..., scopes=("bundled",), exclude_names=...)` 计算，只影响 Task Context。

### 4. `on_intent` 不再输出 skill body

`on_intent` 继续返回：

- confirmed intent
- confidence / reason / phase
- active skill run names
- available tool ids
- state patch

但不再返回 `skill_instructions`，也不再拼接 `VOIDX_SKILL_TOOL_CONTEXT`。新激活的 bundled skill body 已经在稳定 Skill Context Message 中存在。

### 5. `load_skills` 保持 Tool Context marker

`load_skills` 继续用于 project/global skill 按需 body 加载，它仍输出：

```text
VOIDX_SKILL_TOOL_CONTEXT
Scope: current-turn
...
```

历史 ToolMessage 的 strip 逻辑必须保留，因为：

- 旧会话可能已有 `on_intent` 产出的 marker。
- 新会话仍可能有 `load_skills` 产出的 marker。

### 6. Subagent 与主 agent 保持一致

Subagent 也注入同一套 enabled bundled Skill Context Message；agent/intent 只影响 Task Context 的 active 摘要和 run state。

## 不做的事

- 不做 provider 层 `cache_control`。
- 不改变 `load_skills` 行为。
- 不改变 Available Skills section：仍只列非 bundled skill。
- 不绕过 registry 覆盖语义读取 raw bundled skill。
- 不改变 `SkillRunState` 的持久化规则。
- 不删除历史 ToolMessage strip 逻辑。

## 稳定性分析

| 场景 | SystemMessage | Skill Context | 说明 |
|------|:---:|:---:|------|
| 同 turn 多次 LLM call | 不变 | 不变 | 可复用本地编译对象 |
| 跨 turn，skill 组合变了 | 不变 | 不变 | 只有 Task Context 变化 |
| 跨 turn，intent 变了 | 不变 | 不变 | active 摘要变化不影响 Skill Context |
| compaction 触发 | 可能变化 | 不变 | Long Summary 在 SystemMessage 中 |
| bundled skill 文件被修改 | 不变 | 变化 | Skill Context cache key 重建 |
| project/global skill 变化 | 可能变化 | 不变 | Available Skills 可能变化，bundled reference 不受影响 |

## Token 开销

当前 bundled skill 为 8 个，`wc -w` 约 2873 words。常驻后每轮都会发送这些 reference bodies，但换来 Skill Context 的稳定性。

如果未来 bundled skill 数量增长到 15+ 或 body 总量明显膨胀，需要增加体积监控，或拆分为稳定目录摘要 + 按需 body 的二阶段方案。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/voidx/skills/service.py` | 新增 `enabled_bundled_skills()` |
| `src/voidx/skills/context.py` | `render_skill_context()` 增加 reference library wrapper |
| `src/voidx/llm/instruction.py` | `skill_context_for()` 内容改为所有 enabled bundled skill；active 仍动态 |
| `src/voidx/tools/on_intent.py` | 移除 `skill_instructions` 输出和 Tool Context 拼接 |
| `src/voidx/agent/intent_refinement.py` | 不再为 `on_intent` 渲染 bundled skill body |
| `src/voidx/agent/graph/subagent.py` | 与主 agent 使用相同 Skill Context 策略 |
| `src/voidx/agent/agents.py` | 更新 Workflow Skills prompt 说明 |

## 测试计划

| 测试 | 文件 |
|------|------|
| `test_skill_service_returns_enabled_bundled_skills_after_overrides` | `tests/test_skills.py` |
| `test_skill_context_message_contains_all_bundled_skills` | `tests/test_skills.py` |
| `test_skill_context_message_stable_across_intent_changes` | `tests/test_skills.py` |
| `test_skill_context_reference_library_marks_inactive_skills_not_active` | `tests/test_agent/test_runtime_context.py` |
| `test_task_context_only_contains_active_skill_summaries` | `tests/test_agent/test_core_flow.py` |
| `test_on_intent_no_longer_injects_skill_body_in_tool_result` | `tests/test_agent/test_core_flow.py` / `tests/test_tools/test_basic.py` |
| `test_load_skills_still_uses_tool_context_marker` | `tests/test_tools/test_basic.py` |
| `test_subagent_skill_context_matches_orchestrator` | `tests/test_agent/test_core_flow.py` |

## 风险

1. **LLM 注意力分散**
   所有 bundled skill body 常驻，可能让模型过度关注 inactive body。通过 reference library wrapper 和 Task Context active 摘要降低风险。

2. **Token 膨胀**
   当前规模可控。未来 bundled skill 增长后需要重新评估。

3. **覆盖语义**
   同名 project/global skill 覆盖 bundled skill 后，该 bundled body 不再常驻。这符合现有 registry 行为，但需要测试覆盖，避免未来误解。

4. **向后兼容**
   旧 ToolMessage 和 `load_skills` 新 ToolMessage 都可能含 `VOIDX_SKILL_TOOL_CONTEXT`，strip 逻辑必须保留。
