# Persona Prompt 结构化方案

> **Status: Done**
> 日期: 2026-06-17
> 状态: 已完成

## 背景

当前 `src/voidx/agent/agents.py` 中的 prompt 全部是纯字符串常量：

- `BASE_SYSTEM_PROMPT` — 一个大字符串，混合了身份声明、沟通风格、全局规则、工作流运行时规则
- `VOIDX_PROMPT` — persona prompt，混合了协调规则、persona 模型描述、职责、约束
- `PERSONA_PROMPTS` — dict，目前只有 `"voidx"` 一个入口
- 5 个运行时 persona（coordinate/explore/plan/implement/review）没有独立定义，仅作为 `VOIDX_PROMPT` 中的几行描述

问题：
1. 无法程序化地查询或修改单条规则
2. persona 切换时无法精确控制注入哪些规则
3. 新增 persona 需要手动编辑大字符串
4. 无法做条件性规则筛选（如 plan mode 下禁用某些规则）
5. 测试只能做字符串包含检查，无法做结构化断言
6. `VOIDX_PROMPT` 中的 Coordination/Responsibilities/Rules 与 `BASE_SYSTEM_PROMPT` 的 Global Rules 大量重复（10 条中有 3 条完全重复、2 组内部重复），且语义上属于 agent 级别而非 persona 级别

## 目标

将纯字符串 prompt 拆分为 Pydantic 结构化模型：
- `BASE_SYSTEM_PROMPT` → `BaseSystemPrompt` 模型
- `VOIDX_PROMPT` 中的 Coordination/Responsibilities/Rules → 合并入 `BaseSystemPrompt.global_rules`（去重后 5 条新增，消除与 Global Rules 的重复）
- 5 个 persona 各自独立的 `PersonaPrompt` 模型（只定义思维方式）
- Persona 描述有意简化：行为规则由 workflow node 定义覆盖，prompt 只声明思维模式

## 模型定义

### BaseSystemPrompt

```python
class PromptRule(BaseModel):
    label: str   # 加粗前缀，如 "Natural and warm."
    detail: str  # 详细说明

class BaseSystemPrompt(BaseModel):
    identity: str
    communication_style: list[PromptRule]
    global_rules: list[PromptRule]
```

字段映射（当前字符串 → 结构化字段）：

| 字段 | 当前内容 |
|------|---------|
| `identity` | "You are voidx, an autonomous coding agent." |
| `communication_style` | 8 条规则，每条拆为 label + detail |
| `global_rules` | 12 条规则（原 6 条 + 从 AgentPrompt 合并 5 条去重后 + 从 Workflow Runtime 迁入 1 条），每条拆为 label + detail |

`workflow_runtime` 的 4 条元规则从 `BaseSystemPrompt` 中移出，放入独立的 `WorkflowRuntimePrompt`（见下文）。skill 规则迁入 `BaseSystemPrompt.global_rules`。

原 `AgentPrompt`（Coordination/Responsibilities/Rules）整体合并入 `global_rules`，原因：

1. **大量重复** — 10 条规则中有 3 条与 Global Rules 完全重复（"Don't expose persona names" / "Never claim work done until verified" / "Workflow gates take precedence"），2 组内部重复（"Assess before acting" ≈ "Before acting, assess what's known" / "Delegate only when needed" 出现两次）
2. **语义属于 agent 级别** — Coordination/Responsibilities/Rules 描述的是 voidx agent 的行为约束，与 persona 的思维模式无关，无论当前 persona 是 coordinate 还是 implement 都生效
3. **减少层级** — 合并后 L2 Persona 层只保留 PersonaModel（全部 5 个 persona 描述），不再有 AgentPrompt 的中间层

### WorkflowRuntimePrompt

```python
class WorkflowRuntimePrompt(BaseModel):
    rules: list[PromptRule]  # 元规则（4 条）
    node_definitions: str  # 全部 workflow node 的完整定义（由 WorkflowService().context() 生成）
```

独立于 `BaseSystemPrompt`，因为 workflow runtime 规则的性质与身份声明/沟通风格/全局规则不同——它们描述运行时行为约束，由 workflow 系统控制注入时机。

`rules` 包含 4 条元规则（skill 规则迁入 `BaseSystemPrompt.global_rules`）。`node_definitions` 包含全部 workflow node 的完整定义（debug、brainstorm、plan、tdd、verify、review、feedback、design），由 `WorkflowService().context()` 渲染生成，作为静态内容注入 SystemMessage L3 层。

### PersonaPrompt / PersonaModel

```python
class PersonaPrompt(BaseModel):
    name: str
    description: str  # 行为描述，如 "Default. Assess, plan next steps, coordinate work, ..."

class PersonaModel(BaseModel):
    personas: dict[str, PersonaPrompt]  # 全部 5 个 persona 的定义
```

Persona 只定义行为描述，不定义行为规则——规则是 BaseSystemPrompt.global_rules 和 workflow 的事。

`## Persona Model` section 渲染全部 5 个 persona 的描述（静态内容，不随当前 persona 变化）。LLM 通过 `Current Task State` 中的 `Current persona: <name>` 知道当前激活哪个 persona，再对照 Persona Model 中的描述理解该怎么做。

Canonical persona 清单：

| name | description |
|------|-------------|
| coordinate | Default. Assess, plan next steps, coordinate work, delegate when parallel speedup is needed. |
| explore | Read-only evidence gathering and codebase search. Search broadly, report with concrete paths and lines. Do not write or edit files. |
| plan | Design and architecture. Study existing patterns, output structured implementable plans. |
| implement | Build and execute. Write minimal precise edits, run tests to verify. |
| review | Verify and critique. Check correctness, completeness, style, security. Produce PASS/FAIL verdicts. |

## 渲染方法

所有模型都提供 `render() -> str` 方法，输出目标 markdown 格式。

### PromptRule 渲染

```python
def render(self) -> str:
    if self.label:
        return f"**{self.label}** {self.detail}"
    return self.detail
```

### BaseSystemPrompt.render()

```python
def render(self) -> str:
    sections = [self.identity]  # identity 末尾不加换行
    if self.communication_style:
        sections.append("## Communication Style\n\n" + _render_bullets(self.communication_style))
    if self.global_rules:
        sections.append("## Global Rules\n\n" + _render_bullets(self.global_rules))
    return "\n\n".join(sections)
```

注意：`workflow_runtime` 不再由 `BaseSystemPrompt.render()` 输出，由 `WorkflowRuntimePrompt` 独立渲染。`global_rules` 包含原 AgentPrompt 合并后的去重规则。

### WorkflowRuntimePrompt.render()

```python
def render(self) -> str:
    if not self.rules and not self.node_definitions:
        return ""
    parts = []
    if self.rules:
        parts.append("## Workflow Runtime\n\n" + _render_bullets(self.rules))
    if self.node_definitions:
        parts.append(self.node_definitions)
    return "\n\n".join(parts)
```

渲染元规则 + 全部 workflow node 完整定义。`node_definitions` 由 `render_workflow_context()` 预渲染，包含所有 node 的 Goal/Persona/Input/Output/Tools/Gate/Workflow/Exits/Rules。

### PersonaModel.render()

```python
def render(self) -> str:
    lines = [
        "voidx has five thinking modes (personas). The active persona is shown in Current Task State.",
        "Switch persona automatically when entering a workflow node.",
        "- Personas are thinking modes within the same agent, not separate agents. The runtime updates the active persona when workflow nodes change.",
    ]
    for persona in self.personas.values():
        lines.append(f"- **{persona.name}**: {persona.description}")
    return "## Persona Model\n\n" + "\n".join(lines)
```

渲染全部 5 个 persona 的描述，不随当前 persona 变化。Current Task State 中的 `Current persona: <name>` 告诉 LLM 当前激活哪个。

辅助函数：

```python
def _render_bullets(items: list[PromptRule]) -> str:
    return "\n".join(f"- {item.render()}" for item in items)
```

## AgentDef 变更

`AgentDef.persona_prompt` property 移除。当前只有一个 agent（voidx），子 agent 通过 `child_run_agent_def()` 复制 voidx 的 `AgentDef`，但子 agent 不需要 persona_prompt property——它的 prompt 由 `persona_for_agent()` 函数根据 runtime persona 动态获取。

```python
class AgentDef(BaseModel):
    name: str
    description: str
    when_to_use: str
    tools: list[str]
    can_write: bool
    can_delegate: bool
    hidden: bool = False
    model: str | None = None
    mcp_tools: bool = False
    # persona_prompt property 已移除
```

## 数据注册

结构化 prompt 常量放入新模块 `src/voidx/agent/prompts.py`。`src/voidx/agent/agents.py` 只保留 `AgentDef`、agent registry、child-run agent 视图等 agent 定义职责。

### Canonical 规则清单

`BASE_SYSTEM.communication_style` 固定为以下 8 条：

| # | label | detail |
|---|-------|--------|
| 1 | Natural and warm. | Write like a skilled colleague, not a robot. Use contractions, vary sentence length, show personality. |
| 2 | Match the user's language. | If the user writes in Chinese, respond in Chinese. If they write in English, respond in English. Mirror their tone. |
| 3 | Be concise. | One good sentence beats three mediocre ones. The user can ask follow-ups if they want more detail. |
| 4 | Don't explain your internals. | The user doesn't need to know about agents, personas, explore/plan/implement/review, or your architecture. Just help them. If asked "who are you", say "I'm voidx, a coding assistant" — one sentence max. |
| 5 | Say what you're about to do. | Brief heads-up before searching or editing: "Let me check the auth module." — not "I will now delegate to the explore agent." |
| 6 | Summarize results, not process. | After completing work, tell the user what changed and where. Don't narrate which agents you used or how many steps it took. |
| 7 | Acknowledge uncertainty. | If you're not sure, say so. "I think it's auth.py:42, but let me verify" — not "I have medium confidence in this assessment." |
| 8 | Show progress via todo. | Update the todo list so progress is visible. But don't narrate todo updates in your text. |

`BASE_SYSTEM.global_rules` 固定为以下 12 条：

| # | detail |
|---|--------|
| 1 | Use tools for facts about the workspace; do not guess file contents. |
| 2 | Read before editing. Make minimal, precise changes. |
| 3 | Keep user-facing responses concise and focused on outcomes. |
| 4 | Do not expose internal persona names unless the user asks about architecture. |
| 5 | Never claim work is complete until it has been verified. |
| 6 | When Current Task State lists an active workflow gate, that workflow gate takes precedence over persona prompts and delegation rules. |
| 7 | Assess before acting — evaluate what's already known and what's still needed. |
| 8 | Stay aligned with the user's actual goal. |
| 9 | Pick the smallest next action that makes progress toward the goal. |
| 10 | Delegate only when you need to run multiple independent tasks in parallel, or the user explicitly asks for a child agent. Do not delegate single-file reads, simple searches, or straightforward tasks you can do directly. |
| 11 | Subagents do not interact with the user. |
| 12 | skill can return project/global skill bodies for the current turn. |

```python
BASE_SYSTEM = BaseSystemPrompt(
    identity="You are voidx, an autonomous coding agent.",
    communication_style=[
        PromptRule(label="Natural and warm.", detail="Write like a skilled colleague, not a robot. Use contractions, vary sentence length, show personality."),
        PromptRule(label="Match the user's language.", detail="If the user writes in Chinese, respond in Chinese. If they write in English, respond in English. Mirror their tone."),
        PromptRule(label="Be concise.", detail="One good sentence beats three mediocre ones. The user can ask follow-ups if they want more detail."),
        PromptRule(label="Don't explain your internals.", detail="The user doesn't need to know about agents, personas, explore/plan/implement/review, or your architecture. Just help them. If asked \"who are you\", say \"I'm voidx, a coding assistant\" — one sentence max."),
        PromptRule(label="Say what you're about to do.", detail="Brief heads-up before searching or editing: \"Let me check the auth module.\" — not \"I will now delegate to the explore agent.\""),
        PromptRule(label="Summarize results, not process.", detail="After completing work, tell the user what changed and where. Don't narrate which agents you used or how many steps it took."),
        PromptRule(label="Acknowledge uncertainty.", detail="If you're not sure, say so. \"I think it's auth.py:42, but let me verify\" — not \"I have medium confidence in this assessment.\""),
        PromptRule(label="Show progress via todo.", detail="Update the todo list so progress is visible. But don't narrate todo updates in your text."),
    ],
    global_rules=[
        # ── 原 Global Rules（6 条）──
        PromptRule(label="", detail="Use tools for facts about the workspace; do not guess file contents."),
        PromptRule(label="", detail="Read before editing. Make minimal, precise changes."),
        PromptRule(label="", detail="Keep user-facing responses concise and focused on outcomes."),
        PromptRule(label="", detail="Do not expose internal persona names unless the user asks about architecture."),
        PromptRule(label="", detail="Never claim work is complete until it has been verified."),
        PromptRule(label="", detail="When Current Task State lists an active workflow gate, that workflow gate takes precedence over persona prompts and delegation rules."),
        # ── 从 AgentPrompt 合并（去重后 5 条）──
        # "Coordinate without exposing persona names" → 与第 4 条重复，删除
        # "Only declare work done after verification" → 与第 5 条重复，删除
        # "Workflow gates take precedence" → 与第 6 条重复，删除
        # "Assess before acting" ≈ "Before acting, assess what's known" → 合并为一条
        PromptRule(label="", detail="Assess before acting — evaluate what's already known and what's still needed."),
        PromptRule(label="", detail="Stay aligned with the user's actual goal."),
        PromptRule(label="", detail="Pick the smallest next action that makes progress toward the goal."),
        PromptRule(label="", detail="Delegate only when you need to run multiple independent tasks in parallel, or the user explicitly asks for a child agent. Do not delegate single-file reads, simple searches, or straightforward tasks you can do directly."),
        PromptRule(label="", detail="Subagents do not interact with the user."),
        # ── 从 Workflow Runtime 迁入（1 条）──
        PromptRule(label="", detail="skill can return project/global skill bodies for the current turn."),
    ],
})

WORKFLOW_RUNTIME = WorkflowRuntimePrompt(
    rules=[
        PromptRule(label="", detail="voidx has a structured workflow runtime."),
        PromptRule(label="", detail="Current Task State is the activation source for this turn's workflow nodes."),
        PromptRule(label="", detail="Workflow Context messages contain structured workflow node definitions as a stable reference library. Follow ONLY nodes listed as active in Current Task State, unless the user explicitly references another node by name."),
        PromptRule(label="", detail="When a node is not listed as active, its definition is reference only. Do not follow its gate, internal workflow steps, or transition instructions."),
    ],  # 4 条元规则（skill 规则迁入 BaseSystemPrompt.global_rules）
    node_definitions=WorkflowService().context(),  # 全部 workflow node 完整定义，稳定排序
)

PERSONA_MODEL = PersonaModel(
    personas={
        "coordinate": PersonaPrompt(name="coordinate", description="Default. Assess, plan next steps, coordinate work, delegate when parallel speedup is needed."),
        "explore": PersonaPrompt(name="explore", description="Read-only evidence gathering and codebase search. Search broadly, report with concrete paths and lines. Do not write or edit files."),
        "plan": PersonaPrompt(name="plan", description="Design and architecture. Study existing patterns, output structured implementable plans."),
        "implement": PersonaPrompt(name="implement", description="Build and execute. Write minimal precise edits, run tests to verify."),
        "review": PersonaPrompt(name="review", description="Verify and critique. Check correctness, completeness, style, security. Produce PASS/FAIL verdicts."),
    },
)
```

`PersonaModel` 包含全部 5 个 persona 的定义，渲染时全部输出，不随当前 persona 变化。

## 调用方变更

### RuntimeContextBuilder

参数变更：

| 参数 | 之前类型 | 之后类型 | 说明 |
|------|---------|---------|------|
| `base_system_prompt` | `str` | `BaseSystemPrompt` | 内部调用 `.render()` |
| `persona_prompt` | `str` | `str`（不变） | 由 `PERSONA_MODEL.render()` 生成，静态内容，不随当前 persona 变化 |
| 新增 `workflow_runtime` | — | `WorkflowRuntimePrompt | None` | L3 Workflow 层，包含元规则 + 全部 workflow node 完整定义 |
| 移除 `tool_contract` | `str` | — | `## Tool Contract` section 整体移除，由 bind_tools 和 runtime 层兜底 |
| `available_skills` | — | — | 不新增独立 section；继续由 `InstructionService.system()` 追加到 `Project Facts` |
| 合并 `workspace` + RuntimeEnvelope | 分散在两处 | `## Runtime State` section | Workspace Facts + RuntimeEnvelope 合并，消除 workspace 重复 |
| 移除 `skill_context_content` | `str` | — | HumanMessage 路径从未使用，skill 正文统一走 `skill` tool → ToolMessage |
| 移除 `workflow_context_content` | `str` | — | HumanMessage 路径移除，node 定义合并入 `WorkflowRuntimePrompt.node_definitions` |

`persona_prompt` 保持 `str` 类型，由 `PERSONA_MODEL.render()` 生成，渲染全部 5 个 persona 的描述，不随当前 persona 变化。

保持 `_build_stable_sections()` 输出格式不变，但 section name 从 `"Agent Role"` 改为 `"Persona"`。

### core.py

```python
# 之前
base_system_prompt=BASE_SYSTEM_PROMPT,  # str
persona_prompt=persona_prompt_for_llm(agent, ...),  # str

# 之后
base_system_prompt=BASE_SYSTEM,  # BaseSystemPrompt 实例
workflow_runtime=WORKFLOW_RUNTIME,  # WorkflowRuntimePrompt 实例
persona_prompt=persona_prompt(),  # str，静态
```

### subagent.py

```python
# 之前
base_system_prompt=BASE_SYSTEM_PROMPT,
persona_prompt=_agent_prompt(agent_def),

# 之后
base_system_prompt=BASE_SYSTEM,
workflow_runtime=WORKFLOW_RUNTIME,
persona_prompt=persona_prompt(),  # 静态，不随 persona 变化
```

### persona_prompt_for_llm → persona_prompt

函数签名变更：

```python
def persona_prompt() -> str:
    """Return the rendered Persona Model section."""
    return PERSONA_MODEL.render()
```

不再需要 `persona` 参数——Persona Model 渲染全部 5 个 persona 的描述，不随当前 persona 变化。Current Task State 中的 `Current persona: <name>` 告诉 LLM 当前激活哪个。

Child-agent scheduling 规则不再单独渲染成 `## Child-Agent Scheduling` section；并发子代理行为由 `agent` 工具和运行时调度控制，不再需要 `_parallel_subagents_prompt` 函数或 `parallel_subagents_enabled` 参数。

## 改造后的 Context 结构

改造后，LLM 收到的完整消息帧按以下架构组织：

| 层 | ContextSection name | 内容来源 | 说明 |
|----|---------------------|----------|------|
| L1 VoidX Agent | Base System | `BASE_SYSTEM.render()` | 身份、沟通风格、全局规则（含原 AgentPrompt 合并规则） |
| L2 Persona | Persona | `PERSONA_MODEL.render()` | 全部 5 个 persona 的描述（静态，不随当前 persona 变化） |
| L3 Workflow | Workflow Runtime | `WORKFLOW_RUNTIME.render()` | 元规则 + 全部 workflow node 完整定义（原 HumanMessage 合并到 SystemMessage） |
| — | ~~Tool Contract~~ | `agent_def.tool_contract` | **整体移除** |
| L4 Project | Project Facts | `InstructionService.system()` | AGENTS.md 项目指令 + Available Skills |
| L5 Runtime | Runtime State | 动态生成 | 工作区、平台、sandbox、语言、语气 |
| L6 Session | Session Time | 动态生成 | 当前日期时区 |

### Tool Contract 精简

当前 `AgentDef.tool_contract` 包含 5 类信息，其中大部分与 tool binding 或 runtime 层重复：

| 条目 | bind_tools 已提供 | runtime 兜底 | 结论 |
|------|:-:|:-:|------|
| Agent identity: voidx | — | — | **移除** — 只有一个身份，无需声明 |
| Can write files: true | — | ✅ permission engine 拦截 | **移除** — 写操作被 permission 层拦截，LLM 无需预知 |
| Can start child agents: true | — | ✅ agent 工具返回提示 | **移除** — 子代理调用 agent 工具时由 runtime 返回约束提示 |
| Available tools: clarify, ... | ✅ | — | **移除** — bind_tools 的 JSON schema 已包含 |
| MCP tools: available when... | — | — | **移除** — bind_tools 已提供，无需 prompt 提示 |
| Constraint: must not start... | — | ✅ agent 工具返回提示 | **移除** — 子 agent 约束由 runtime 兜底 |

**结论：`AgentDef.tool_contract` 整体移除。** 所有信息要么由 bind_tools 隐式提供，要么由 runtime 层（permission engine、agent 工具返回值）兜底，无需在 prompt 中重复声明。

### MCP Tool 层

MCP 工具通过 `bind_tools()` 注入 LLM 请求（`mcp__` 前缀的工具定义），LLM 可以直接看到并调用。当前 `Tool Contract` 中的 `MCP tools: available when configured` 提示是冗余的——如果 MCP 工具存在，LLM 自然能在工具列表中看到；如果不存在，这条提示反而误导。

**处理方式：不新增独立的 MCP Tool section。** MCP 工具的可见性完全由 `bind_tools()` 控制，`AgentDef.mcp_tools` 字段决定是否在工具过滤时包含 `mcp__` 前缀的工具定义。LLM 不需要额外的 prompt 提示。

### Skill 的归属

Skill 正文通过 `skill` 工具调用注入，返回 `ToolMessage`（带 `VOIDX_SKILL_TOOL_CONTEXT` 标记）。不再使用独立的 `HumanMessage (skill context)` 路径——`render_skill_context()` 和 `skill_context_content` 参数移除，统一走 tool 调用更符合 LLM 的交互模型（LLM 主动决定何时加载 skill body）。

保留 `render_skill_tool_context()`、`SKILL_TOOL_CONTEXT_MARKER`、`has_skill_tool_context()`、`strip_skill_tool_context()`：

- `render_skill_tool_context()` 是 `skill` 工具当前输出技能正文的唯一包装入口。
- `SKILL_TOOL_CONTEXT_MARKER` 用来区分“当前轮可遵循的 skill body”和普通工具输出。
- `has_skill_tool_context()` / `strip_skill_tool_context()` 用于在后续轮次剥离历史 ToolMessage 中的完整 skill body，避免技能正文被反复带入语义历史，减少上下文污染和 token 浪费。

只删除 `VOIDX_SKILL_CONTEXT` HumanMessage 路径。旧 session 中已持久化的 `VOIDX_SKILL_CONTEXT` 不做兼容处理。

### 完整消息帧（主 agent、coordinate persona、非 plan mode）

```
SystemMessage
├─ VOIDX_RUNTIME_CONTEXT          ← _render_sections() 固定前缀
│
├─ ── L1: VoidX Agent ─────────────────────────────────────────────
│
├─ ## Base System                 ← BASE_SYSTEM.render()
│  ├─ You are voidx, an autonomous coding agent.
│  ├─ ## Communication Style
│  │  ├─ - **Natural and warm.** Write like a skilled colleague, not a robot.
│  │  │    Use contractions, vary sentence length, show personality.
│  │  ├─ - **Match the user's language.** If the user writes in Chinese, respond in Chinese.
│  │  │    If they write in English, respond in English. Mirror their tone.
│  │  ├─ - **Be concise.** One good sentence beats three mediocre ones. The user can ask
│  │  │    follow-ups if they want more detail.
│  │  ├─ - **Don't explain your internals.** The user doesn't need to know about agents,
│  │  │    personas, explore/plan/implement/review, or your architecture. Just help them.
│  │  │    If asked "who are you", say "I'm voidx, a coding assistant" — one sentence max.
│  │  ├─ - **Say what you're about to do.** Brief heads-up before searching or editing:
│  │  │    "Let me check the auth module." — not "I will now delegate to the explore agent."
│  │  ├─ - **Summarize results, not process.** After completing work, tell the user what
│  │  │    changed and where. Don't narrate which agents you used or how many steps it took.
│  │  ├─ - **Acknowledge uncertainty.** If you're not sure, say so. "I think it's auth.py:42,
│  │  │    but let me verify" — not "I have medium confidence in this assessment."
│  │  └─ - **Show progress via todo.** Update the todo list so progress is visible.
│  │       But don't narrate todo updates in your text.
│  ├─ ## Global Rules
│  │  ├─ - Use tools for facts about the workspace; do not guess file contents.
│  │  ├─ - Read before editing. Make minimal, precise changes.
│  │  ├─ - Keep user-facing responses concise and focused on outcomes.
│  │  ├─ - Do not expose internal persona names unless the user asks about architecture.
│  │  ├─ - Never claim work is complete until it has been verified.
│  │  ├─ - When Current Task State lists an active workflow gate, that workflow gate takes
│  │  │    precedence over persona prompts and delegation rules.
│  │  ├─ - Assess before acting — evaluate what's already known and what's still needed.
│  │  ├─ - Stay aligned with the user's actual goal.
│  │  ├─ - Pick the smallest next action that makes progress toward the goal.
│  │  ├─ - Delegate only when you need to run multiple independent tasks in parallel,
│  │  │    or the user explicitly asks for a child agent. Do not delegate single-file
│  │  │    reads, simple searches, or straightforward tasks you can do directly.
│  │  ├─ - Subagents do not interact with the user.
│  │  └─ - (Child-agent scheduling rules as PromptRule)
│
├─ ── L2: Persona ─────────────────────────────────────────────────
│
├─ ## Persona                     ← PERSONA_MODEL.render()
│  └─ ## Persona Model
│     ├─ voidx has five thinking modes (personas). The active persona is
│     │    shown in Current Task State.
│     ├─ Switch persona automatically when entering a workflow node.
│     ├─ - Personas are thinking modes within the same agent, not separate
│     │    agents. The runtime updates the active persona when workflow
│     │    nodes change.
│     ├─ - **coordinate**: Default. Assess, plan next steps, coordinate work,
│     │    delegate when parallel speedup is needed.
│     ├─ - **explore**: Read-only evidence gathering and codebase search.
│     │    Search broadly, report with concrete paths and lines.
│     │    Do not write or edit files.
│     ├─ - **plan**: Design and architecture. Study existing patterns,
│     │    output structured implementable plans.
│     ├─ - **implement**: Build and execute. Write minimal precise edits,
│     │    run tests to verify.
│     └─ - **review**: Verify and critique. Check correctness, completeness,
│          style, security. Produce PASS/FAIL verdicts.
│
├─ ── L3: Workflow ─────────────────────────────────────────────────
│
├─ ## Workflow Runtime            ← WORKFLOW_RUNTIME.render() 元规则
│  ├─ - voidx has a structured workflow runtime.
│  ├─ - Current Task State is the activation source for this turn's
│  │    workflow nodes.
│  ├─ - Workflow Context messages contain structured workflow node definitions
│  │    as a stable reference library. Follow ONLY nodes listed as active in
│  │    Current Task State, unless the user explicitly references another
│  │    node by name.
│  ├─ - When a node is not listed as active, its definition is reference only.
│  │    Do not follow its gate, internal workflow steps, or transition
│  │    instructions.
│
│  ┌─ Workflow Node Definitions   ← WORKFLOW_RUNTIME.node_definitions
│  │  (由 render_workflow_context() 预渲染)
│  ├─ ## Workflow Node: debug
│  │  └─ Goal / Persona / Input / Output / Tools / Gate / Workflow / Exits / Rules
│  ├─ ## Workflow Node: brainstorm
│  │  └─ Goal / Persona / Input / Output / Tools / Gate / Workflow / Exits / Rules
│  ├─ ## Workflow Node: plan
│  │  └─ ...
│  ├─ ## Workflow Node: tdd
│  │  └─ ...
│  ├─ ## Workflow Node: verify
│  │  └─ ...
│  ├─ ## Workflow Node: review
│  │  └─ ...
│  ├─ ## Workflow Node: feedback
│  │  └─ ...
│  └─ ## Workflow Node: design
│     └─ ...
│
├─ ── L4: Project ──────────────────────────────────────────────────
│
├─ ## Project Facts               ← InstructionService.system()
│  └─ Instructions from: project AGENTS.md
│     └─ (AGENTS.md content + Available Skills)
│
├─ ── L5: Runtime ───────────────────────────────────────────────────
│
├─ ## Runtime State               ← 合并 Workspace Facts + RuntimeEnvelope
│  ├─ - Current workspace: <workspace>
│  ├─ - Platform: macOS arm64 (Apple Silicon)
│  ├─ - Sandbox: workspace-write
│  ├─ - Approval policy: untrusted
│  ├─ - Language instruction: Prefer responding in Chinese (Simplified)
│  │    unless the user explicitly asks otherwise.
│  └─ - Tone instruction: Prefer short answers. Remove filler and avoid
│       restating obvious context.
│
├─ ── L6: Session ───────────────────────────────────────────────────
│
└─ ## Session Time
   └─ 2026-06-17 CST

... (对话历史)
```
HumanMessage (用户消息 + task context)   ← _prepend_task_context() 将 task context 合并进最后一条 HumanMessage
├─ VOIDX_RUNTIME_CONTEXT
│  └─ ## Current Task State
│     ├─ - Current persona: coordinate
│     ├─ - Intent: coding
│     ├─ - Goal type: design
│     ├─ - Goal: ...
│     ├─ - Active workflow nodes: ...
│     ├─ - Workflow route: ...
│     └─ - Active todo: N items
│        ├─ in_progress: ...
│        └─ pending: ...
├─ ## Task Context
└─ (用户原始输入)

... (更早的对话历史)

### 为什么 workflow node 定义移入 SystemMessage

改造前，workflow node 的完整定义放在独立 HumanMessage (workflow context) 中，与 SystemMessage 中的 4 条元规则分离。改造后，两者合并到 SystemMessage 的 L3 Workflow 层——元规则在前，node 定义紧随其后。

**移除独立 HumanMessage 的原因：**

1. **语义完整** — 元规则和它所约束的数据应该在一起。LLM 看到 "follow ONLY nodes listed as active" 时，需要同时看到 node 定义才能理解规则的含义。分离在两个 message 中增加了理解负担。
2. **减少消息帧复杂度** — 少一个 HumanMessage，消息帧更简洁。

**Skill context 统一走 tool 调用** — `render_skill_context()` 和 `skill_context_content` HumanMessage 路径移除。旧 session 中已持久化的 `VOIDX_SKILL_CONTEXT` 不做兼容处理。Skill 正文统一通过 LLM 调用 `skill` tool → `ToolMessage`（`VOIDX_SKILL_TOOL_CONTEXT` 标记）注入。`render_skill_tool_context()`、`SKILL_TOOL_CONTEXT_MARKER` 和历史 ToolMessage 剥离逻辑保留，用于标记当前轮 skill body 并在后续轮次剥离旧技能正文。

### 与改造前的差异

| 位置 | 改造前 | 改造后 |
|------|--------|--------|
| Section 命名 | `## Agent Role` | `## Persona` |
| Base System 内容 | `BASE_SYSTEM_PROMPT` 纯字符串，含 Workflow Runtime | `BASE_SYSTEM.render()`，不含 Workflow Runtime |
| Workflow Runtime | 嵌在 Base System section 内，node 定义在独立 HumanMessage | L3 包含元规则 + 全部 workflow node 完整定义（原 HumanMessage 合并到 SystemMessage） |
| Persona 内容 | `VOIDX_PROMPT` 纯字符串（含 Coordination/Responsibilities/Rules + 全部 5 个 persona 描述）+ `_parallel_subagents_prompt` 拼接 | `PERSONA_MODEL.render()`（全部 5 个 persona 描述）；Coordination/Responsibilities/Rules 合并入 BaseSystemPrompt.global_rules |
| Persona 描述 | 嵌在 VOIDX_PROMPT 的 `## Persona Model` 中，5 个 persona 列表 | `## Persona Model` 保留全部 5 个 persona 描述，由 `PersonaModel.render()` 渲染 |
| Tool Contract | `## Tool Contract` section（identity + 权限 + 工具列表 + MCP + 约束） | **整体移除** — 由 bind_tools 和 runtime 层兜底 |
| Workflow node 定义 | 独立 HumanMessage (workflow context) | 合并到 L3 Workflow Runtime section 内 |
| Available Skills | 嵌在 `## Project Facts` 内 | 仍嵌在 `## Project Facts` 内（由 `InstructionService.system()` 追加） |
| Skill 正文 | HumanMessage (skill context)（`VOIDX_SKILL_CONTEXT` 标记） | **移除** — 统一走 `skill` tool → ToolMessage（`VOIDX_SKILL_TOOL_CONTEXT` 标记） |
| Workspace + RuntimeEnvelope | `## Workspace Facts` + HumanMessage(RuntimeEnvelope) | 合并为 `## Runtime State`（L6），消除 workspace 重复 |

**有意的内容变化：**
1. 改造前 `## Persona Model` 列出全部 5 个 persona 的行为描述，改造后保留全部 5 个描述，由 `PersonaModel.render()` 渲染。Current Task State 中的 `Current persona: <name>` 告诉 LLM 当前激活哪个 persona。
2. 改造前 Coordination/Responsibilities/Rules 在 `## Agent Role` section 中，改造后合并入 `## Global Rules`。去重后 Global Rules 从 6 条变为 12 条——3 条完全重复的规则只保留一份，2 组内部重复的规则合并为一组，1 条 skill 规则从 Workflow Runtime 迁入。

### 子 Agent 的 Context 差异

子 agent 通过 `child_run_agent_def()` 复制 voidx 的 AgentDef，context 拼接逻辑相同，但有以下差异：

- `## Runtime Constraints` section：注入 `CHILD_RUN_CONSTRAINTS`
- `## Persona` 中：`persona_prompt()` 渲染全部 5 个 persona 描述（静态，与主 agent 相同）
- Tool binding: 工具列表受限（CHILD_RUN_TOOLS），`can_delegate: false`，由 runtime 层控制

## 兼容性

- `BaseSystemPrompt.render()` + `WorkflowRuntimePrompt.render()` 拼接后与目标 Base System 文本一致（包含 `You are voidx, an autonomous coding agent.`、合并后的 Global Rules、独立 Workflow Runtime）
- **AgentPrompt 整体合并入 BaseSystemPrompt.global_rules**：Coordination/Responsibilities/Rules 的 10 条规则去重后 5 条合并入 Global Rules，L2 Persona 层不再包含 Coordination/Responsibilities/Rules
- Persona 描述保持不变：全部 5 个 persona 的行为描述由 `PersonaModel.render()` 渲染，与本文 canonical persona 表一致
- **Workflow context 合并**：HumanMessage (workflow context) 移除，全部 workflow node 完整定义并入 SystemMessage L3 Workflow 层（`WorkflowRuntimePrompt.node_definitions`）
- **Child-Agent Scheduling 简化**：不再单独渲染 `## Child-Agent Scheduling` section，`_parallel_subagents_prompt` 函数移除；并发子代理行为由 `agent` 工具和运行时调度控制
- **Tool Contract 整体移除**：`AgentDef.tool_contract` property 删除，`## Tool Contract` section 不再渲染。所有信息由 bind_tools 和 runtime 层兜底
- **Workflow node 定义移入 SystemMessage**：从独立 HumanMessage (workflow context) 移入 L3 Workflow Runtime section，元规则与定义数据合并
- **Available Skills 归属变更**：不再拆成独立 L4；继续由 `InstructionService.system()` 追加到 `## Project Facts`
- **Skill context HumanMessage 移除**：`render_skill_context()` 和 `skill_context_content` HumanMessage 路径移除。Skill 正文统一走 `skill` tool → ToolMessage（`VOIDX_SKILL_TOOL_CONTEXT` 标记）；ToolMessage 标记与历史剥离逻辑保留
- **Workspace Facts + RuntimeEnvelope 合并**：`## Workspace Facts` section 和 HumanMessage(RuntimeEnvelope) 合并为 `## Runtime State` section，消除 workspace 字段重复
- `CHILD_RUN_CONSTRAINTS` 和 `PLAN_MODE_APPEND` 暂时保持纯字符串，后续可纳入结构化
- `AgentDef.persona_prompt` property 移除，调用方改用 `persona_prompt()`
- `PERSONA_PROMPTS` dict 移除，替换为 `PERSONA_MODEL`（PersonaModel 实例）

## 实施阶段

### Phase 1 — 结构化 prompt 模型

- 新增 `src/voidx/agent/prompts.py`，定义 `PromptRule`、`BaseSystemPrompt`、`WorkflowRuntimePrompt`、`PersonaPrompt`、`PersonaModel`。
- 在 `prompts.py` 中注册 `BASE_SYSTEM`、`WORKFLOW_RUNTIME`、`PERSONA_MODEL` 和 `persona_prompt()`。
- `WorkflowRuntimePrompt.node_definitions` 使用 `WorkflowService().context()` 生成，沿用现有 workflow node 稳定排序。
- `agents.py` 移除 `BASE_SYSTEM_PROMPT`、`VOIDX_PROMPT`、`PERSONA_PROMPTS` 和 prompt 拼接 helper，仅保留 agent 定义职责。
- 测试使用显式目标快照或结构化断言，不再把旧字符串常量当 oracle。

### Phase 2 — Runtime context 重排

- `RuntimeContextBuilder` 接入 `BaseSystemPrompt` / `WorkflowRuntimePrompt`，把 workflow node 定义合并进 SystemMessage。
- 移除 `Tool Contract` section。
- 保持 Available Skills 由 `InstructionService.system()` 注入 `Project Facts`，不新增独立 section。
- 合并 `Workspace Facts` 和 RuntimeEnvelope 为 `Runtime State`。
- 移除 `workflow_context_content` / `skill_context_content` HumanMessage 路径。
- 删除旧 `VOIDX_SKILL_CONTEXT` HumanMessage helpers；保留 `VOIDX_SKILL_TOOL_CONTEXT` ToolMessage helpers。

## 测试策略

1. **渲染一致性测试**：`BASE_SYSTEM.render() + "\n\n" + WORKFLOW_RUNTIME.render()` 输出 == 显式维护的目标快照（包含 `autonomous coding agent`、合并后的 12 条 Global Rules、独立 Workflow Runtime）
2. **workflow runtime 渲染测试**：`WORKFLOW_RUNTIME.render()` 输出 == 目标 `## Workflow Runtime` section + workflow node 完整定义
3. **PersonaModel 渲染测试**：`PERSONA_MODEL.render()` 输出 == 显式维护的目标 Persona Model 快照（含全部 5 个 persona 描述）
4. **PromptRule 渲染测试**：有 label 时输出 `**label** detail`，无 label 时输出 `detail`
5. **结构化断言测试**：可以断言 `BASE_SYSTEM.communication_style` 有 8 条规则、`BASE_SYSTEM.global_rules` 有 12 条规则、`PERSONA_MODEL.personas` 有 5 个条目
6. **persona 描述一致性测试**：每个 `PersonaPrompt.description` 与本文 canonical persona 表一致
7. **去重验证测试**：`BASE_SYSTEM.global_rules` 中不包含与原 AgentPrompt 重复的规则（"Don't expose persona names" / "Never claim work done" / "Workflow gates take precedence" 只出现一次）
8. **现有测试更新后保持通过**：明确覆盖 intentional deltas（Tool Contract 移除、workflow context 进 SystemMessage、Available Skills 保持在 Project Facts、skill HumanMessage 路径移除）

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `src/voidx/agent/prompts.py` | 新增 PromptRule、BaseSystemPrompt、WorkflowRuntimePrompt、PersonaPrompt、PersonaModel 模型；注册 BASE_SYSTEM、WORKFLOW_RUNTIME、PERSONA_MODEL；提供 persona_prompt() |
| `src/voidx/agent/agents.py` | 移除 BASE_SYSTEM_PROMPT、VOIDX_PROMPT、PERSONA_PROMPTS、AgentDef.persona_prompt property、AgentDef.tool_contract property、_parallel_subagents_prompt、persona_prompt_for_llm；保留 AgentDef 和 agent registry |
| `src/voidx/agent/runtime_context.py` | RuntimeContextBuilder 新增 workflow_runtime 参数；base_system_prompt 参数类型从 str 改为 BaseSystemPrompt；workflow_runtime section 含元规则 + node 完整定义；移除 Tool Contract section 渲染；不再新增独立 available_skills 参数；合并 Workspace Facts + RuntimeEnvelope 为 Runtime State section；移除 workflow_context_content HumanMessage 及相关编译逻辑；移除 skill_context_content 参数及相关 HumanMessage 编译逻辑 |
| `src/voidx/agent/graph/core.py` | 从 `voidx.agent.prompts` 导入 BASE_SYSTEM、WORKFLOW_RUNTIME、persona_prompt；更新 persona_prompt_for_llm 调用为 persona_prompt() |
| `src/voidx/agent/graph/subagent.py` | 同上；移除 _agent_prompt 函数，改用 persona_prompt() |
| `tests/test_agent/test_agents.py` | 新增：PromptRule 渲染测试、BaseSystemPrompt 渲染一致性测试（含合并后 global_rules）、WorkflowRuntimePrompt 渲染测试、PersonaModel 渲染测试、结构化断言测试、persona 描述一致性测试、去重验证测试 |
| `tests/test_agent/test_core_flow.py` | 更新导入和断言 |
| `src/voidx/skills/context.py` | 移除 `render_skill_context()`、`SKILL_CONTEXT_MARKER`、`is_skill_context_content()`、`skill_context_cache_key()`；保留 `render_skill_tool_context()`、`SKILL_TOOL_CONTEXT_MARKER`、`has_skill_tool_context()`、`strip_skill_tool_context()` |
| `src/voidx/agent/graph/compaction_coordinator.py` | 移除 `is_skill_context_content` 对 HumanMessage 的 prefix 判断逻辑 |
