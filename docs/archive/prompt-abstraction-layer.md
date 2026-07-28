> **Status: Done** — Archived on 2026-07-26.

---
name: prompt-abstraction-layer
display_name: 提示词抽象层设计
description: 将系统提示词从硬编码单例升级为规则池 + profile 组合模式，每条规则带 name 标识，profile 按需组合并校验工具能力
doc_type: tech-design
audience: human+llm
status: proposed
source_design: docs/design/agent-runtime-chat.md
---

# 提示词抽象层设计

## 1. 问题

当前系统提示词以 `BASE_SYSTEM` 单例为根。`PromptPolicy`（chat 闭环时引入）只能控制 persona / workflow / task_state / profile_directive 四个段的有无，**无法覆盖 Base System 本身**。

结果：chat 模式仍注入 coding 向的 identity（"autonomous coding agent"）、Communication Style（含 `todo_progress` / `summarize_results`）和 Global Rules（含 Workspace Rules / Delegation Rules）。这些内容对 chat 语义无意义——LLM 没有对应工具能力执行它们。

## 2. 目标

- 每条规则有唯一 `name` 标识，构成**规则池**。
- 每个 profile 通过 `name` 列表**组合**规则，而非硬编码整个 Base System 变体。
- 每条规则声明其**工具能力依赖**（`requires`），profile 组合时校验该 profile 的 tool_view 能满足依赖。
- 不在 `_prepare_with_stream` 或 `RuntimeContextBuilder` 中写 `if profile_id == "chat"` 分支。
- 保持 coding 路径行为不变（零回归）。
- 未来新增 profile（loop / goal）只需声明 name 列表，不改框架代码。

## 3. 现状

### 3.1 提示词组装链路

```
_prepare_with_stream (execution.py:1406)
  └─ RuntimeContextBuilder.__init__ (runtime_context.py:159)
       ├─ base_system_prompt  ← build_base_system(lang)  [不可 profile 化]
       ├─ persona_prompt      ← persona_prompt() / policy 覆盖
       ├─ workflow_runtime    ← WORKFLOW_RUNTIME / policy 覆盖
       ├─ profile_directive   ← policy 新增段
       ├─ task_state          ← suppress_task_state 控制
       └─ RuntimeEnvelope     ← workspace/platform/sandbox [profile 无关]
```

`build_base_system` 有两个调用点：
- `execution.py:1464` — 主 agent 路径
- `subagent.py:128` — 子代理路径

两者都调 `build_base_system(language)` 不传 `base_system` 参数，走默认 `BASE_SYSTEM`。子代理是 coding 专用，用默认 coding prompt。

### 3.2 当前 PromptPolicy 能力

| 属性 | 作用 | coding | chat |
|---|---|---|---|
| `persona_prompt` | Persona 段 | None=沿用 | ""=抑制 |
| `workflow_runtime` | Workflow Runtime 段 | None=沿用 | ""=抑制 |
| `task_state_section` | Current Task State 段 | None=沿用 | ""=抑制 |
| `profile_directive` | 新增段 | None=无 | chat directive 文本 |
| **`base_system_spec`** | **Base System 段** | **不存在** | **不存在** |

### 3.3 规则 name 标识现状

Communication Style 的 8 条规则**有 name**：

| name | label | 语义 |
|---|---|---|
| `language` | Match the user's language | 通用 |
| `tone` | Natural and warm | 通用 |
| `concise` | Be concise | 通用 |
| `internals` | Don't expose internals | 通用 |
| `progress_preamble` | Say what you're about to do | 通用（任何有工具的 profile） |
| `summarize_results` | Summarize outcomes | 通用（行为约束，不依赖特定工具） |
| `uncertainty` | Acknowledge uncertainty | 通用 |
| `todo_progress` | Show progress via todo | coding 向（需要 todo 工具） |

Global Rule Sections 的规则**全无 name**：

| section | 规则数 | 语义 |
|---|---|---|
| Runtime Rules | 1 | coding 向（需要 workflow 运行时） |
| Workspace Rules | 4 | coding 向（需要文件读写工具） |
| Verification Rules | 1 | 通用（任何会执行操作的 profile） |
| Collaboration Rules | 2 | 通用 |
| Delegation Rules | 1 | coding 向（需要 agent 工具） |

### 3.4 工具 id 现状

Chat 的工具集定义在 `chat_policy.py`：

```python
_ALWAYS_BOUND_TOOLS = frozenset({"websearch", "webfetch", "mcp"})
_LOCAL_READ_TOOLS = frozenset({"read", "glob", "grep", "lsp"})
_ESCAPE_TOOLS = frozenset({"bash", "powershell", "write", "manage", "replace", "git", "agent", "subagent"})
```

- chat 无 workspace：`bound_tool_ids = {"websearch", "webfetch", "mcp"}`
- chat 有 workspace：`bound_tool_ids = {"websearch", "webfetch", "mcp", "read", "glob", "grep", "lsp"}`
- coding：全量工具集（含 `write`, `replace`, `bash`, `todo`, `agent` 等）

todo 工具的 id 是 `"todo"`（见 `todo_state.py:249`、`runtime_guards.py:28`）。

## 4. 方案

### 4.1 核心思路

三步：

1. **规则池**：所有规则声明为带 `name` 的独立常量，附带 `requires` 声明工具能力依赖。
2. **Profile 组合**：每个 profile 声明自己选用的 rule name 列表（communication_style_names + global_rule_names），框架按 name 从池中取规则组装 `BaseSystemPrompt`。
3. **能力校验**：组装时校验 profile 的 tool_view 能满足每条规则的 `requires`，不满足则跳过并记录 warning。

### 4.2 规则池结构

**不新建 `ProfileRule` 类型**——直接在现有 `PromptRule` 上加 `requires` 字段（F1 修正）：

```python
class PromptRule(BaseModel):
    name: str = ""
    label: str = ""
    detail: str
    # 该规则生效需要 profile 具备的工具能力（tool_id 集合）
    # 空集合 = 无能力要求，任何 profile 可用
    requires: set[str] = Field(default_factory=set)
```

`requires` 用 `set[str]` 而非 `frozenset[str]`——pydantic v2 对 `frozenset` 的 JSON 序列化支持不完整（F10 修正）。

规则池定义为模块级常量，按 section 分组：

```python
# ── Communication Style 规则池 ──
STYLE_RULES: dict[str, PromptRule] = {
    "language":         PromptRule(name="language",         label="...", detail="..."),
    "tone":             PromptRule(name="tone",             label="...", detail="..."),
    "concise":          PromptRule(name="concise",          label="...", detail="..."),
    "internals":        PromptRule(name="internals",        label="...", detail="..."),
    "progress_preamble":PromptRule(name="progress_preamble",label="...", detail="..."),
    "summarize_results":PromptRule(name="summarize_results",label="...", detail="..."),
    # requires 为空集：通用行为约束，任何 profile 可用（F6 修正）
    "uncertainty":      PromptRule(name="uncertainty",      label="...", detail="..."),
    "todo_progress":    PromptRule(name="todo_progress",    label="...", detail="...",
                          requires={"todo"}),  # 需要 todo 工具（F2 修正）
}

# ── Global Rules 规则池，按 section 分组 ──
GLOBAL_RULE_SECTIONS: dict[str, dict[str, PromptRule]] = {
    "Runtime Rules": {
        "workflow_gates": PromptRule(name="workflow_gates", detail="..."),
        # requires 为空集：workflow 运行时不是 tool_id，requires 只覆盖 tool_id（F8 澄清）
    },
    "Workspace Rules": {
        "workspace_facts":   PromptRule(name="workspace_facts",   detail="...",
                                requires={"read", "glob", "grep"}),
        "read_before_edit":  PromptRule(name="read_before_edit",  detail="...",
                                requires={"read", "replace"}),
        "smallest_change":   PromptRule(name="smallest_change",   detail="...",
                                requires={"replace"}),
        "preserve_dirty":    PromptRule(name="preserve_dirty",    detail="..."),
    },
    "Verification Rules": {
        "fresh_verification":PromptRule(name="fresh_verification",detail="..."),
    },
    "Collaboration Rules": {
        "min_questions":     PromptRule(name="min_questions",     detail="..."),
        "follow_requests":   PromptRule(name="follow_requests",   detail="..."),
    },
    "Delegation Rules": {
        "delegate_independent":PromptRule(name="delegate_independent", detail="...",
                                requires={"agent"}),
    },
}
```

### 4.3 `requires` 语义边界

`requires` 只覆盖 **tool_id 能力依赖**——"该 profile 根本没有这个工具，规则无意义"的情况。

不覆盖的依赖类型（F8 澄清）：
- **workflow 运行时依赖**（如 `workflow_gates`）：workflow 运行时不是 tool_id，无法用 `requires` 表达。chat 通过 `CHAT_PROFILE_SPEC` 不选 `workflow_gates` 来排除。
- **workspace 绑定依赖**（如 `workspace_facts` 需要 workspace 才有意义）：通过 tool_id 间接表达——chat 无 workspace 时 `read`/`glob`/`grep` 不在 `bound_tool_ids` 中，`requires` 不满足，规则被跳过。

### 4.4 Profile 组合声明

每个 profile 声明选用的 rule name：

```python
class BaseSystemProfile(BaseModel):
    """Profile-scoped base system prompt assembly spec."""
    identity: str
    style_names: list[str]           # 从 STYLE_RULES 取
    global_section_names: dict[str, list[str]]  # section_title -> [rule_name]
```

```python
CODING_PROFILE_SPEC = BaseSystemProfile(
    identity="You are voidx, an autonomous coding agent.",
    style_names=["language","tone","concise","internals","progress_preamble",
                 "summarize_results","uncertainty","todo_progress"],
    global_section_names={
        "Runtime Rules":       ["workflow_gates"],
        "Workspace Rules":     ["workspace_facts","read_before_edit","smallest_change","preserve_dirty"],
        "Verification Rules":  ["fresh_verification"],
        "Collaboration Rules": ["min_questions","follow_requests"],
        "Delegation Rules":    ["delegate_independent"],
    },
)

CHAT_PROFILE_SPEC = BaseSystemProfile(
    identity="You are voidx, a conversational assistant.",
    style_names=["language","tone","concise","internals","progress_preamble",
                 "summarize_results","uncertainty"],
    # summarize_results 保留：通用行为约束，chat 用 websearch 后同样需要总结（F6 修正）
    # 去掉 todo_progress（需要 todo 工具，chat 没有）
    global_section_names={
        "Verification Rules":  ["fresh_verification"],
        # 保留：websearch 结果仍需验证
        "Collaboration Rules": ["min_questions","follow_requests"],
        # 去掉 Runtime Rules（无 workflow 运行时）、Workspace Rules（无文件写工具）、Delegation Rules（无 agent 工具）
    },
)
```

### 4.5 能力校验

组装时，框架用 profile 的可用工具集校验每条规则的 `requires`：

```python
def assemble_base_system(
    spec: BaseSystemProfile,
    available_tools: set[str] | None = None,
) -> BaseSystemPrompt:
    """Assemble BaseSystemPrompt from spec.

    When available_tools is provided, skip rules whose tool requirements
    are not met. When available_tools is None, skip capability validation
    entirely (coding path — zero regression, F7 修正).
    """
    style_rules = []
    for name in spec.style_names:
        rule = STYLE_RULES.get(name)
        if rule is None:
            raise KeyError(f"Unknown style rule: {name!r}")
        if available_tools is not None and rule.requires and not rule.requires <= available_tools:
            logger.warning("skip rule %s: requires %s, available %s", name, rule.requires, available_tools)
            continue
        style_rules.append(rule)

    sections = []
    for section_title, rule_names in spec.global_section_names.items():
        section_rules = []
        for name in rule_names:
            rule = GLOBAL_RULE_SECTIONS.get(section_title, {}).get(name)
            if rule is None:
                raise KeyError(f"Unknown rule in section {section_title!r}: {name!r}")
            if available_tools is not None and rule.requires and not rule.requires <= available_tools:
                logger.warning("skip rule %s: requires %s, available %s", name, rule.requires, available_tools)
                continue
            section_rules.append(rule)
        if section_rules:
            sections.append(PromptSection(title=section_title, rules=section_rules))

    return BaseSystemPrompt(
        identity=spec.identity,
        communication_style=style_rules,
        global_rule_sections=sections,
    )
```

**KeyError 处理**（F9 修正）：name 拼写错误是编程错误，抛 `KeyError` 而非静默跳过——fail fast 优于隐藏 bug。

### 4.6 可用工具集来源（F3 修正）

```python
# ── coding 路径 ──
# coding 不做能力校验，available_tools=None
# 原因：coding 用全量工具集，所有 requires 都满足，校验是多余开销
# 且若 coding 的工具集因配置变化而不完整，跳过规则会改变行为，违反零回归（F7）
base_system_prompt = build_base_system(
    self.config.user_profile.language,
    base_system=assemble_base_system(CODING_PROFILE_SPEC, available_tools=None),
)

# ── chat 路径 ──
# chat 从 ChatToolView.bound_tool_ids 获取可用工具集
tool_view = getattr(self, "_active_chat_tool_view", None)
available_tools = set(tool_view.bound_tool_ids) if tool_view is not None else None
base_system_prompt = build_base_system(
    self.config.user_profile.language,
    base_system=assemble_base_system(CHAT_PROFILE_SPEC, available_tools=available_tools),
)
```

`_active_chat_tool_view` 已在 `turn_runner.py:116` 设置：`host._active_chat_tool_view = getattr(context, "tool_view", None)`。

### 4.7 PromptPolicy 扩展

```python
class PromptPolicy(Protocol):
    base_system_spec: BaseSystemProfile | None   # 新增
    persona_prompt: str | None
    workflow_runtime: str | None
    task_state_section: str | None
    profile_directive: str | None
```

- `CodingPromptPolicy.base_system_spec` = `None`（coding 路径 fallback 到 `CODING_PROFILE_SPEC`，`available_tools=None`）
- `ChatPromptPolicy.base_system_spec` = `CHAT_PROFILE_SPEC`

### 4.8 _prepare_with_stream 改动

```python
active_profile = getattr(self, "_active_profile", None)
prompt_policy = getattr(active_profile, "prompt_policy", None)

# 选择 base system spec
base_spec = (
    prompt_policy.base_system_spec
    if prompt_policy is not None and prompt_policy.base_system_spec is not None
    else CODING_PROFILE_SPEC
)

# 获取可用工具集
tool_view = getattr(self, "_active_chat_tool_view", None)
available_tools = set(tool_view.bound_tool_ids) if tool_view is not None else None

# 组装 base system
base_system_prompt = build_base_system(
    self.config.user_profile.language,
    base_system=assemble_base_system(base_spec, available_tools=available_tools),
)
```

### 4.9 subagent.py 改动（F5 补充）

`subagent.py:128` 当前调 `build_base_system(language)` 不传 `base_system`。subagent 是 coding 专用，改为：

```python
base_system_prompt=build_base_system(
    context_config.user_profile.language,
    base_system=assemble_base_system(CODING_PROFILE_SPEC, available_tools=None),
),
```

或保持原样 `build_base_system(language)`——因为 `BASE_SYSTEM` 常量将改为 `assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)` 的返回值（见 8.1），两者等价。**推荐保持原样**，减少改动面。

### 4.10 渲染后的 chat 提示词（方案后）

```
## Base System
You are voidx, a conversational assistant.

## Communication Style
- **Match the user's language.** ...
- **Natural and warm.** ...
- **Be concise.** ...
- **Don't expose internals.** ...
- **Say what you're about to do.** ...
- **Summarize outcomes.** ...
- **Acknowledge uncertainty.** ...

## Global Rules
### Verification Rules
- Never claim work is complete, fixed, passing, or safe until fresh verification has run in this turn.

### Collaboration Rules
- Ask only the minimum questions needed to proceed, preferably one at a time.
- Follow user requests unless they conflict with higher-priority instructions or safety constraints.

## Profile Directive
You are operating in chat profile...

## Runtime State
...
```

对比当前：identity 从 "coding agent" 变为 "conversational assistant"；Communication Style 从 8 条减到 7 条（保留 `summarize_results`，去掉 `todo_progress`）；Global Rules 从 5 个 section 减到 2 个（保留 Verification + Collaboration）。

## 5. 能力校验的运行时行为

`requires` 校验在**组装时**（每次 turn 开始）执行，不是静态声明时。原因：

- 同一 profile 的可用工具可能因 workspace 绑定不同而变化（chat 无 workspace 时无 `read`/`glob`/`grep`，有 workspace 时有）。
- `workspace_facts` 的 `requires={"read","glob","grep"}`：chat 无 workspace 时被跳过，有 workspace 时保留。

校验失败时**跳过规则并记录 warning**，不抛异常——提示词降级优于 turn 失败。

**例外**：coding 路径 `available_tools=None`，跳过能力校验，保证零回归（F7）。

## 6. 不做的事

- **不做规则继承**：profile 不写 "继承 coding 再去掉 X"，而是显式声明自己的 name 列表。显式优于隐式。
- **不做规则模板引擎**：不引入 Jinja/字符串模板，保持 pydantic model + `render()` 的声明式结构。
- **不改 `RuntimeEnvelope`**：workspace/platform/sandbox 是 profile 无关的运行时事实，所有 profile 共享。
- **不改 `RuntimeContextBuilder` 签名**：`base_system_prompt` 参数已支持 `BaseSystemPrompt` 实例，组装逻辑在传入前完成。
- **不改 `build_base_system` 签名**：保持 `(language, *, base_system=None)` 不变。

## 7. 改动范围

| 文件 | 改动 |
|---|---|
| `src/voidx/agent/prompts.py` | `PromptRule` 加 `requires: set[str]` 字段；新增 `BaseSystemProfile`、`STYLE_RULES`、`GLOBAL_RULE_SECTIONS`、`CODING_PROFILE_SPEC`、`CHAT_PROFILE_SPEC`、`assemble_base_system`；给现有 Global Rules 补 name；`BASE_SYSTEM` 改为由 `assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)` 生成 |
| `src/voidx/agent/domain/prompt_policy.py` | `PromptPolicy` 加 `base_system_spec` 属性；`ChatPromptPolicy` 返回 `CHAT_PROFILE_SPEC` |
| `src/voidx/agent/infrastructure/langgraph/execution.py` | `_prepare_with_stream` 读取 `policy.base_system_spec`，调用 `assemble_base_system` |
| `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py` | 保持原样（`BASE_SYSTEM` 常量等价于 `assemble_base_system(CODING_PROFILE_SPEC, None)`，见 4.9） |
| `src/tests/test_agent/test_prompts.py` | 更新 `test_base_system_structure`：`global_rules` 的 name 不再全为 `""`，改为断言具体 name；新增 `assemble_base_system` 单元测试 |
| `src/tests/test_agent/test_chat_prompt_policy.py` | 断言 `ChatPromptPolicy.base_system_spec` 是 `CHAT_PROFILE_SPEC`；断言 identity 和 rules 内容 |
| `src/tests/test_agent/graph/test_chat_e2e.py` | 断言 chat system prompt 含 "conversational assistant"、不含 "Workspace Rules"、"Delegation Rules"、"todo_progress" |
| `src/tests/test_agent/test_prompt_assembly.py`（新） | 测试 `assemble_base_system`：`available_tools=None` 时保留所有规则；全工具可用时保留所有规则；缺工具时跳过对应规则；name 拼写错误时抛 KeyError |

## 8. 迁移策略

### 8.1 向后兼容

`BASE_SYSTEM` 常量改为 `assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)` 的返回值，外部引用点（`build_base_system` 默认参数、`subagent.py`）无需改动。`build_base_system` 签名不变。

`assemble_base_system` 返回值在相同输入下稳定（纯函数，无随机性），不破坏 `RuntimeContextBuilder.build_incremental` 的 `stable_prefix_key` 缓存（R2 确认）。

### 8.2 分步实施

1. **加 requires 字段**：`PromptRule` 加 `requires: set[str]` 字段，默认空集。现有构造点无需改动（默认值）。
2. **补 name**：给现有 Global Rules 的所有规则加 `name` 字段（纯数据补充，不改逻辑）。
3. **建规则池**：提取 `STYLE_RULES` / `GLOBAL_RULE_SECTIONS` 常量。
4. **建 spec**：定义 `CODING_PROFILE_SPEC` / `CHAT_PROFILE_SPEC`。
5. **建组装器**：实现 `assemble_base_system` + 能力校验。
6. **重构 BASE_SYSTEM**：改为 `assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)`。
7. **接 policy**：`PromptPolicy` 加 `base_system_spec`，`_prepare_with_stream` 调用组装器。
8. **更新测试**：`test_prompts.py` 断言更新 + 新增 `test_prompt_assembly.py`。
9. **测试**：单元测试 + e2e 断言。

## 9. 风险

- **`BASE_SYSTEM` 重构**：现有 `BASE_SYSTEM` 是直接构造的常量，改为 `assemble_base_system(...)` 生成后，渲染输出必须与原来**完全一致**（coding 回归）。需逐条比对 `name`/`label`/`detail` 和列表顺序。`assemble_base_system(CODING_PROFILE_SPEC, None)` 不做能力校验，直接取 spec 中所有规则，顺序与 `CODING_PROFILE_SPEC` 声明一致——需确保声明顺序与现有 `BASE_SYSTEM` 一致。
- **`requires` 语义**：`requires` 是"工具能力依赖"不是"工具使用前提"。例如 `fresh_verification` 无 `requires`（纯行为约束），但 chat 用 websearch 时仍应遵守。`requires` 只用于"该 profile 根本没有这个工具，规则无意义"的情况。
- **规则池膨胀**：未来规则增多后 `STYLE_RULES` / `GLOBAL_RULE_SECTIONS` 可能变大。可接受——规则池是单文件内的声明式数据，比分散在多个 profile 变体里更易维护。
- **subagent 路径**：`subagent.py` 保持调 `build_base_system(language)` 不变，依赖 `BASE_SYSTEM` 常量的等价性。需在测试中覆盖 subagent 路径的 prompt 不变（R4 补充）。

## 10. 验收标准

- [ ] 所有 Communication Style 和 Global Rules 规则有唯一 `name`
- [ ] `assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)` 渲染结果与现有 `BASE_SYSTEM.render()` 完全一致
- [ ] `assemble_base_system(CHAT_PROFILE_SPEC, chat_tools)` 渲染结果 identity 为 "conversational assistant"
- [ ] chat 渲染的 system prompt 不含 "Workspace Rules"、"Delegation Rules"、"todo_progress"
- [ ] chat 渲染的 system prompt 保留 "Verification Rules"、"Collaboration Rules"、"summarize_results"
- [ ] chat 无 workspace 时 `workspace_facts` 被跳过（requires 不满足）
- [ ] chat 有 workspace 时 `workspace_facts` 保留（requires 满足）
- [ ] coding 渲染的 system prompt 与改动前完全一致（回归保护，`available_tools=None` 跳过校验）
- [ ] subagent 路径的 system prompt 与改动前完全一致（回归保护）
- [ ] `assemble_base_system` 对未知 name 抛 `KeyError`
- [ ] `./test.py --backend` 全量绿