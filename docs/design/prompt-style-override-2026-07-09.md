---
name: prompt-style-override
display_name: Prompt Style 通用化改造
description: 将 Communication Style 规则结构化为 name+value，支持按 lang 开关动态替换，移除 runtime_context 冗余 Language instruction
doc_type: tech-design
---

# Prompt Style 通用化改造 — 技术设计文档

## Context

当前系统提示词的 Communication Style 是硬编码的模块级常量 `BASE_SYSTEM`，无法根据用户设置动态调整。语言偏好通过两条路径注入，存在冗余：

1. `prompts.py` 中 Communication Style 的 "Match the user's language" 规则——**永远存在，不可变**
2. `runtime_context.py` 的 `_render_envelope` 注入的 "Language instruction: Prefer responding in X"——**受 lang 开关控制**

用户希望 lang 开关直接控制系统提示词本身，而非仅在 runtime_context 补充。同时希望改造是**通用的**——每条 style 规则都能通过 name 标识、按需替换，方便以后扩展其他维度的覆盖逻辑（tone、locale 等）。

## Goals and Non-Goals

### Goals

- `PromptRule` 增加 `name` 字段，作为规则标识，支持按名查找和替换
- Communication Style 的每条规则都有稳定 `name`，可被覆盖（完整清单见下文）
- 提供 `build_base_system(language)` 工厂函数，根据 lang 开关返回定制化的 `BaseSystemPrompt`
- `auto`/空值时返回默认 `BASE_SYSTEM`（保持 "Match the user's language" 原文）
- 非 auto 时用对应语言的规则替换 `name="language"` 那条
- 支持自定义语言码（如 `fr`、`de`、`pt-BR`）：未知但非空的 language 不回退 no-op，而是生成通用语言覆盖规则
- 移除 `runtime_context.py` 中 `_render_envelope` 的 Language instruction 注入
- 将 `_LANGUAGE_LABELS` 映射表从 `runtime_context.py` 迁移到 `prompts.py`，消除循环导入；`slash/profile.py` 改为从 `prompts.py` 导入

### Non-Goals

- 不改造 `global_rules`（全局规则不需要按 lang 切换）
- 不改造 tone 机制（tone 仍通过 runtime_context 注入，独立于本次改造；但本次分析确认了 tone 改造的可行性和方向，见下文"Tone 改造可行性分析"）
- 不做多维度组合覆盖（如 lang + tone 同时覆盖同一条规则）

## Architecture

```
用户设置 lang
    │
    ▼
GraphLlmMixin._prepare_with_stream / run_subagent
    │
    │  config.user_profile.language
    ▼
build_base_system(language)  ← 工厂函数（prompts.py）
    │
    │  auto/空 → 返回 BASE_SYSTEM（默认）
    │  已知语言 → 用 _LANGUAGE_STYLE_OVERRIDES[lang] 替换 name="language" 的规则
    │  未知语言 → 生成自定义 PromptRule(name="language", label="Respond in <code>.", ...)
    │           返回新的 BaseSystemPrompt 实例
    ▼
base_system_prompt: BaseSystemPrompt
    │
    ▼
RuntimeContextBuilder(..., base_system_prompt=base_system_prompt)
    │
    │  _render_prompt_input() 调用 .render()
    ▼
ContextSection(name="Base System", content=base_system_prompt.render())
    │
    ▼
系统提示词（Communication Style 中已包含语言偏好）
```

runtime_context 的 `_render_envelope` 不再注入 Language instruction，语言偏好由系统提示词承载。`RuntimeContextBuilder` 只负责渲染上下文，不负责根据 `config` 定制系统提示词；语言覆盖在调用方完成。

## Tone 改造可行性分析

> 本节为前瞻性分析，不在本次改造范围内。记录结论供后续迭代参考。

### 现状

tone 当前通过 `runtime_context.py` 的 `_render_envelope` 注入（`_TONE_LABELS` + `_tone_instruction`），与 lang 改造前的模式完全对称。`_TONE_LABELS` 定义了 6 种 tone：

| tone key | label | detail |
|----------|-------|--------|
| concise | Concise | Prefer short answers. Remove filler and avoid restating obvious context. |
| friendly | Friendly | Keep phrasing warm and approachable while staying concrete. |
| formal | Formal | Use polished, structured phrasing and avoid casual wording. |
| direct | Direct | Be direct and practical. Lead with the answer or action. |
| technical | Technical | Use precise domain terminology. Prefer concrete specs and implementation details over broad summaries. |
| casual | Casual | Use relaxed conversational phrasing without losing technical accuracy. |

### 关键发现："Natural and warm." 本身就是默认 tone

`BASE_SYSTEM` Communication Style 的第一条规则：

> **Natural and warm.** Write like a skilled colleague, not a robot. Use contractions, vary sentence length, show personality.

这条规则本质上是一个**固定 tone**——"natural and warm"。它与 `_TONE_LABELS` 中的 `friendly` + `casual` 高度重叠：

- "skilled colleague, not a robot" ≈ `casual` 的 "relaxed conversational phrasing"
- "show personality" + "warm" ≈ `friendly` 的 "warm and approachable"

### 指令冲突风险

如果 tone 开关走系统提示词替换，但 "Natural and warm." 保留不动，用户选了 `direct` 或 `formal` 时会出现**指令冲突**：LLM 同时收到"show personality, be warm"和"be direct, no fluff"，语义矛盾。

### 改造方向

tone 改造比 lang 更干净——不需要新增泛化锚点规则，直接将 "Natural and warm." 作为 `name="tone"` 的替换锚点：

- `auto`/空 → 保留 "Natural and warm."（默认 tone）
- `direct` → 替换为 `PromptRule(name="tone", label="Direct.", detail="Be direct and practical. Lead with the answer or action.")`
- `formal` → 替换为 `PromptRule(name="tone", label="Formal.", detail="Use polished, structured phrasing and avoid casual wording.")`
- ...

与 lang 的对比：

| 维度 | lang | tone |
|------|------|------|
| 默认规则语义 | 泛化指令（"Match the user's language"） | 具体 tone（"Natural and warm."） |
| 替换锚点 | 需要泛化规则作为锚点 | 默认规则本身就是锚点 |
| auto 时行为 | 保留泛化指令 | 保留默认 tone |
| 改造复杂度 | 需要新增 name 字段 + 泛化规则 | 只需给现有规则加 name="tone" |

### 后续改造所需变更

若未来实施 tone 改造，需要：

1. 给 "Natural and warm." 规则加 `name="tone"`
2. 将 `_TONE_LABELS` 迁移到 `prompts.py`（与 `_LANGUAGE_LABELS` 同步迁移）
3. 扩展 `build_base_system` 签名为 `build_base_system(language="", tone="")`
4. 移除 `_render_envelope` 的 Tone instruction 注入（474-476 行）
5. 清理 `_tone_instruction()` 死代码（`runtime_context.py:568`）

本次 lang 改造已为 tone 改造铺好基础设施：`PromptRule.name` 字段、工厂函数模式、映射表迁移方向。

## Data Model

### Communication Style rule names（新增）

`BASE_SYSTEM.communication_style` 中每条规则都获得稳定 `name`。`name` 是内部覆盖锚点，不参与渲染；后续扩展 lang、tone、locale 等覆盖逻辑时，只按 `name` 匹配。

| 现有 label | name | 覆盖用途 |
|------------|------|----------|
| `Natural and warm.` | `tone` | 后续 tone 改造的默认锚点；本次不覆盖 |
| `Match the user's language.` | `language` | 本次 lang 覆盖锚点 |
| `Be concise.` | `concise` | 保留结构化锚点，当前不覆盖 |
| `Don't explain your internals.` | `internals` | 保留结构化锚点，当前不覆盖 |
| `Say what you're about to do.` | `progress_preamble` | 保留结构化锚点，当前不覆盖 |
| `Summarize results, not process.` | `summarize_results` | 保留结构化锚点，当前不覆盖 |
| `Acknowledge uncertainty.` | `uncertainty` | 保留结构化锚点，当前不覆盖 |
| `Show progress via todo.` | `todo_progress` | 保留结构化锚点，当前不覆盖 |

命名原则：使用稳定语义名，不绑定英文 label 文案；label 可重写或翻译，`name` 不应随文案变化。

### PromptRule（修改）

```
PromptRule
├── name: str = ""        # 新增：内部 runtime 标识，如 "language"、"concise"。空 = 静态规则
├── label: str = ""       # 渲染文案，面向 LLM 的加粗前缀，可随意调整
└── detail: str           # 渲染文案，规则正文
```

`name` 与 `label` 职责分离：
- `name` — 内部稳定标识，工厂函数按 `name` 匹配替换，不参与渲染，不会因文案调整而断裂
- `label` — 面向 LLM 的渲染文案，可随意调整，不影响匹配逻辑

`name` 不影响 `render()` 输出。

```
BaseSystemPrompt
├── identity: str
├── communication_style: list[PromptRule]
└── global_rules: list[PromptRule]
```

### 覆盖映射表（新增）

```
_LANGUAGE_STYLE_OVERRIDES: dict[str, PromptRule]
├── "zh-cn" → PromptRule(name="language", label="使用中文回复。", detail="...")
├── "zh"    → PromptRule(name="language", label="使用中文回复。", detail="...")
├── "zh-tw" → PromptRule(name="language", label="使用繁體中文回覆。", detail="...")
├── "en"    → PromptRule(name="language", label="Respond in English.", detail="...")
├── "en-us" → PromptRule(name="language", label="Respond in English.", detail="...")
├── "ja"    → PromptRule(name="language", label="日本語で応答してください。", detail="...")
└── "ko"    → PromptRule(name="language", label="한국어로 응답하세요.", detail="...")
```

key 与 `_LANGUAGE_LABELS` 的 key 一一对应（迁移后两者同在 `prompts.py`，共享同一组语言标识）。两者职责不同，刻意保持分离：

- `_LANGUAGE_LABELS`: `dict[str, tuple[str, str]]` — `(display_name, tag)`，用于 `/lang` 交互选择列表渲染（`slash/profile.py`）
- `_LANGUAGE_STYLE_OVERRIDES`: `dict[str, PromptRule]` — 系统提示词规则覆盖，用于 `build_base_system`

不合并的原因：`_LANGUAGE_LABELS` 的 value 是 UI 展示用的 `(名称, 标签)` 元组，`_LANGUAGE_STYLE_OVERRIDES` 的 value 是 LLM 提示词规则，消费方和生命周期不同。合并会导致 UI 层依赖 `PromptRule` 类型，增加耦合。

## API Contract

### build_base_system(language: str = "", *, base_system: BaseSystemPrompt | None = None) -> BaseSystemPrompt

- **Path/Signature**: `voidx.agent.prompts.build_base_system(language: str = "", *, base_system: BaseSystemPrompt | None = None) -> BaseSystemPrompt`
- **Request**: `language` — 用户语言设置，来自 `config.user_profile.language`；`base_system` — 可选的自定义基础 prompt，默认 `BASE_SYSTEM`，用于测试中传入缺少 `name="language"` 锚点的 prompt 验证 `ValueError`
- **Response**: `BaseSystemPrompt` 实例
- **Behavior**:
  - `language` 经 `.strip()` 后用 `.lower()` 查询映射表（与现有 `_language_target` 保持一致：`text = value.strip()` → `_LANGUAGE_LABELS.get(text.lower())`，支持 `"zh-CN"` 大写输入）
  - 归一化后为空 → 返回 `base_system`（默认 `BASE_SYSTEM`）引用本身（不创建新实例）
  - 在 `_LANGUAGE_STYLE_OVERRIDES` 中 → 返回新的 `BaseSystemPrompt` 实例，`name="language"` 的规则被替换为已知语言文案
  - 不在 `_LANGUAGE_STYLE_OVERRIDES` 中但非空 → 返回新的 `BaseSystemPrompt` 实例，`name="language"` 的规则被替换为自定义语言文案，保留 `/lang` 支持 `fr`、`de`、`pt-BR` 等任意语言码的现有能力
  - 自定义语言文案示例：`PromptRule(name="language", label=f"Respond in {language}.", detail=f"Prefer responding in {language} unless the user explicitly asks otherwise.")`
- **Errors**: 未知语言不报错，使用自定义语言覆盖规则；若 `base_system.communication_style` 缺少 `name="language"` 锚点，抛出 `ValueError`，避免语言覆盖静默 no-op

### _render_envelope（修改）

- **Path/Signature**: `voidx.agent.runtime_context._render_envelope(envelope) -> str`
- **Change**: 移除 language 相关的 4 行代码（470-473），不再注入 Language instruction
- **Preserved**: tone 指令保持不变

### 死代码清理

移除 Language instruction 后，以下函数失去唯一调用点，需一并清理：

- `_language_target()`（`runtime_context.py:560`）— 唯一调用点在 `_render_envelope:472`，移除后成为死代码
- `_language_display()`（`runtime_context.py:551`）— 当前已无调用者，本次一并清理

`_LANGUAGE_LABELS` 迁移到 `prompts.py` 后，`runtime_context.py` 中不再需要该常量定义，但 `_language_display` 若有保留价值可迁移到 `prompts.py`；当前无调用者，直接删除。

### 调用点改造

运行时构建系统提示词的入口必须从固定 `BASE_SYSTEM` 改为 `build_base_system(config.user_profile.language)`：

- `src/voidx/agent/graph/core/llm.py:111` — 主 agent 上下文构建
- `src/voidx/agent/graph/subagent.py:125` — 子 agent 上下文构建

两处都能拿到 `config` 对象，`config.user_profile.language` 已可用。

其他 `BASE_SYSTEM` 直接引用保持默认语义，不参与动态覆盖：

- `src/tests/test_agent/test_prompts.py` 和 `src/tests/test_agent/graph/test_graph_setup_prompts.py` 可继续断言默认 `BASE_SYSTEM.render()` 内容
- graph 测试中的 `BASE_SYSTEM` import 仅用于测试默认 prompt 组成，不代表生产运行时入口
- 若未来新增生产路径并直接传 `BASE_SYSTEM` 给 `RuntimeContextBuilder`，测试必须失败并要求改为 `build_base_system(...)`

> **备选方案**：`RuntimeContextBuilder.__init__` 已接收 `config` 参数，也可让调用方继续传 `BASE_SYSTEM`，由 `RuntimeContextBuilder` 内部根据 `config.user_profile.language` 调 `build_base_system`。好处是调用方无需关心语言覆盖逻辑，未来扩展 tone 维度时也只改一处。当前选择在调用方处理是为了保持 `RuntimeContextBuilder` 的职责单一（渲染上下文，不负责提示词定制），且 `base_system_prompt` 参数已支持传入 `BaseSystemPrompt` 实例。

### `_LANGUAGE_LABELS` 迁移的连带改造

`_LANGUAGE_LABELS` 从 `runtime_context.py` 迁移到 `prompts.py` 后，`slash/profile.py` 的导入路径需同步更新：

- `src/voidx/agent/slash/profile.py:26` — `from voidx.agent.runtime_context import _LANGUAGE_LABELS` 改为 `from voidx.agent.prompts import _LANGUAGE_LABELS`

`slash/profile.py` 用 `_LANGUAGE_LABELS` 渲染 `/lang` 交互选择列表（`profile.py:30`），迁移后行为不变，仅导入路径改变。`_current_language_label()` 当前只返回 `profile.language or "auto"`，不依赖 `_language_display()`，因此 `_language_display()` 可随 `runtime_context.py` 的语言注入逻辑一并删除。

## 测试影响

| 测试文件 | 断言内容 | 改动 |
|---------|---------|------|
| `test_task_state_rendering.py:168` | `"Language instruction: Prefer responding in Chinese (Simplified)"` in messages | 移除 runtime envelope 断言，改为检查系统提示词中包含对应语言规则，且 Runtime State 不再包含 Language instruction |
| `test_prepare_workflow.py:349` | 同上 | 同上 |
| `test_prompts.py` | `BASE_SYSTEM.render()` 包含 Communication Style | 保持默认 `BASE_SYSTEM` 断言，并新增 `build_base_system` 单元测试 |
| `test_graph_setup_prompts.py` | `BASE_SYSTEM.render()` 断言 | 保持默认 prompt 断言；如覆盖主 agent 构建路径，则断言 `_prepare_with_stream` 传入的 SystemMessage 使用 `build_base_system` 后的内容 |
| `slash/profile.py` 相关 | `/lang` 交互命令用 `_LANGUAGE_LABELS` 渲染选择列表 | 当前无直接测试；迁移导入路径后行为不变，手动验证 `/lang` 命令即可 |

新增测试：
- `build_base_system("")` 返回 `BASE_SYSTEM` 本身
- `build_base_system("zh-CN")` 返回的实例中 `name="language"` 规则被替换为中文版本
- `build_base_system("unknown")` 返回新实例，`name="language"` 规则被替换为自定义语言版本，不回退 no-op
- `build_base_system("pt-BR")` 保留 `/lang` 自定义语言码现有行为，系统提示词包含 `pt-BR` 偏好
- `build_base_system(...)` 在缺少 `name="language"` 锚点时抛出 `ValueError`，防止静默失效
- 主 agent 构建出的 SystemMessage 包含语言覆盖规则，且 Runtime State 中不再包含 `Language instruction:`
- 子 agent 构建出的 SystemMessage 同样包含语言覆盖规则，覆盖 `run_subagent` 的独立入口

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 未知语言代码 | 生成自定义语言覆盖规则，保留现有 `/lang` 任意语言码能力 |
| `language` 为空字符串 | 回退默认 `BASE_SYSTEM` |
| `BASE_SYSTEM.communication_style` 中没有 `name="language"` 的规则 | 抛出 `ValueError`，防止实现漏加锚点时静默 no-op |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 加 `name` 字段，与 `label` 职责分离 | 用 `label` 做匹配 | `label` 是面向 LLM 的渲染文案，可能被调整；`name` 是内部稳定标识，解耦匹配与文案，避免文案改动导致匹配断裂 |
| 工厂函数返回新实例而非修改 `BASE_SYSTEM` | 直接修改模块级常量 | `BASE_SYSTEM` 作为不可变默认值，避免副作用，支持并发安全 |
| 将 `_LANGUAGE_LABELS` 迁移到 `prompts.py` | 在 `prompts.py` 导入 `runtime_context`（循环依赖）；新建独立映射 | `runtime_context` 已依赖 `prompts`，反向导入会循环；迁移后依赖方向一致，单一来源 |
| 移除 runtime_context Language instruction | 保留作为运行时状态显示 | 用户明确指出冗余，语言偏好由系统提示词承载即可 |
| `name` 不影响 `render()` | `name` 参与渲染 | `name` 是内部标识，不应出现在提示词文本中 |
| 在调用方调 `build_base_system` | 在 `RuntimeContextBuilder` 内部调 | `RuntimeContextBuilder` 职责是渲染上下文，不应承担提示词定制；调用方已持有 `config`，且 `base_system_prompt` 参数已支持传入 `BaseSystemPrompt` 实例。未来若扩展 tone，可重新评估 |
| 缺少 `name="language"` 锚点时失败 | 返回默认 `BASE_SYSTEM` 或跳过替换 | 语言覆盖是本次核心行为，静默 no-op 会掩盖实现错误；用异常和测试保护结构完整性 |
| 不给 `global_rules` 加 `name` | 所有 `PromptRule` 都加 `name` | 本次只覆盖 Communication Style；`global_rules` 不参与 lang 覆盖，保持静态规则可减少无意义迁移 |

## Open Questions

- [ ] 覆盖规则的 `detail` 文案需要确认（特别是 ja、ko 的翻译质量）
- [x] 未知语言码策略已确认：保留 `/lang` 自定义语言码能力，未知但非空的 language 生成自定义覆盖规则
- [x] Communication Style rule name 清单已补充，避免实现时临时命名
- [x] `global_rules` 不加 `name`：当前 Non-Goal，结构上虽支持但无覆盖需求
