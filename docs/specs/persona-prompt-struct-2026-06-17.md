# Persona Prompt 结构化方案

> 日期: 2026-06-17
> 状态: 待实施

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

## 目标

将纯字符串 prompt 拆分为 Pydantic 结构化模型：
- `BASE_SYSTEM_PROMPT` → `BaseSystemPrompt` 模型
- 5 个 persona 各自独立的 `PersonaPrompt` 模型
- 保持渲染输出与当前 markdown 格式完全兼容

## 模型定义

### BaseSystemPrompt

```python
class BaseSystemPrompt(BaseModel):
    identity: str
    communication_style: list[str]
    global_rules: list[str]
    workflow_runtime: list[str]
```

字段映射（当前字符串 → 结构化字段）：

| 字段 | 当前内容 |
|------|---------|
| `identity` | "You are voidx, a coding agent that lives in the terminal." |
| `communication_style` | 8 条规则（Natural and warm / Match the user's language / ...） |
| `global_rules` | 6 条规则（Use tools for facts / Read before editing / ...） |
| `workflow_runtime` | 5 条规则（voidx has a structured workflow runtime / ...） |

### PersonaPrompt

```python
class PersonaPrompt(BaseModel):
    name: str
    essence: str  # 思维本质：一句话定义这个 persona 怎么想
```

Persona 只定义思维方式，不定义行为规则——规则是 workflow 的事。

5 个 persona 的定义：

| name | essence |
|------|---------|
| coordinate | Converge on decisions — extract clear goals from vague intent, weigh constraints, decide the next action |
| explore | Diverge on possibilities — exhaustively enumerate paths, evidence, and risks around the goal, never prune prematurely |
| plan | Converge on a path — decompose the chosen approach into ordered, verifiable steps |
| implement | Converge on action — execute the plan step by step, seeking local optimum at each step, never skip ahead |
| review | Diverge on scrutiny — step outside implement's local perspective and re-examine the whole picture |

## 渲染方法

两个模型都提供 `render() -> str` 方法，输出与当前格式完全兼容的 markdown：

### BaseSystemPrompt.render()

```python
def render(self) -> str:
    sections = []
    sections.append(self.identity)
    if self.communication_style:
        sections.append("## Communication Style\n\n" + _render_bullets(self.communication_style))
    if self.global_rules:
        sections.append("## Global Rules\n\n" + _render_bullets(self.global_rules))
    if self.workflow_runtime:
        sections.append("## Workflow Runtime\n\n" + _render_bullets(self.workflow_runtime))
    return "\n\n".join(sections)
```

### PersonaPrompt.render()

```python
def render(self) -> str:
    return f"## Thinking Mode\n\n{self.essence}"
```

辅助函数：

```python
def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)
```

## AgentDef 变更

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

    @property
    def persona_prompt(self) -> str:
        persona = PERSONA_DEFINITIONS.get(self.name, {}).get("coordinate")
        return persona.render() if persona else ""
```

## 数据注册

```python
BASE_SYSTEM = BaseSystemPrompt(
    identity="You are voidx, a coding agent that lives in the terminal.",
    communication_style=[
        "Natural and warm. Write like a skilled colleague, not a robot. ...",
        "Match the user's language. ...",
        # ... 8 条
    ],
    global_rules=[
        "Use tools for facts about the workspace; do not guess file contents.",
        # ... 6 条
    ],
    workflow_runtime=[
        "voidx has a structured workflow runtime.",
        # ... 5 条
    ],
)

PERSONA_DEFINITIONS: dict[str, dict[str, PersonaPrompt]] = {
    "voidx": {
        "coordinate": PersonaPrompt(name="coordinate", essence="Converge on decisions — extract clear goals from vague intent, weigh constraints, decide the next action"),
        "explore": PersonaPrompt(name="explore", essence="Diverge on possibilities — exhaustively enumerate paths, evidence, and risks around the goal, never prune prematurely"),
        "plan": PersonaPrompt(name="plan", essence="Converge on a path — decompose the chosen approach into ordered, verifiable steps"),
        "implement": PersonaPrompt(name="implement", essence="Converge on action — execute the plan step by step, seeking local optimum at each step, never skip ahead"),
        "review": PersonaPrompt(name="review", essence="Diverge on scrutiny — step outside implement's local perspective and re-examine the whole picture"),
    },
}
```

## 调用方变更

### RuntimeContextBuilder

`base_system_prompt` 参数类型从 `str` 改为 `BaseSystemPrompt`，内部调用 `.render()` 获取字符串。

`persona_prompt` 参数类型从 `str` 改为 `PersonaPrompt`，内部调用 `.render()` 获取字符串。

保持 `_build_stable_sections()` 输出格式不变。

### core.py / subagent.py

```python
# 之前
base_system_prompt=BASE_SYSTEM_PROMPT,  # str

# 之后
base_system_prompt=BASE_SYSTEM,  # BaseSystemPrompt 实例
```

```python
# 之前
persona_prompt = persona_prompt_for_llm(agent, ...)

# 之后
persona_prompt = persona_for_agent(agent, runtime_persona)
# 返回 PersonaPrompt 实例
```

### persona_prompt_for_llm → persona_for_agent

函数签名变更：

```python
def persona_for_agent(
    agent: AgentDef,
    persona: str,
    *,
    parallel_subagents_enabled: bool = False,
) -> PersonaPrompt:
    """Return the PersonaPrompt for the given agent and runtime persona."""
    definitions = PERSONA_DEFINITIONS.get(agent.name, {})
    prompt = definitions.get(persona)
    if prompt is None:
        raise ValueError(f"No persona prompt for agent={agent.name} persona={persona}")
    # parallel subagents 规则注入方式待定：
    # 方案 A: 在 render() 后追加字符串（保持当前行为）
    # 方案 B: 作为 PersonaPrompt 的动态字段
    return prompt
```

## 兼容性

- `render()` 输出与当前 markdown 格式完全一致，LLM 看到的内容不变
- `CHILD_RUN_CONSTRAINTS` 和 `PLAN_MODE_APPEND` 暂时保持纯字符串，后续可纳入结构化
- `_parallel_subagents_prompt` 暂时保持纯字符串追加，后续可纳入 PersonaPrompt 动态字段

## 测试策略

1. **渲染一致性测试**：`BASE_SYSTEM.render()` 输出 == 当前 `BASE_SYSTEM_PROMPT` 字符串
2. **persona 渲染测试**：每个 `PersonaPrompt.render()` 输出 `## Thinking Mode\n\n{essence}`
3. **结构化断言测试**：可以断言 `BASE_SYSTEM.communication_style` 有 8 条规则
4. **persona 查找测试**：`persona_for_agent(agent, "explore")` 返回正确的 PersonaPrompt
5. **现有测试保持通过**：不改变 LLM 看到的内容

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `src/voidx/agent/agents.py` | 新增 BaseSystemPrompt、PersonaPrompt 模型；替换字符串常量为模型实例；更新 AgentDef.persona_prompt |
| `src/voidx/agent/runtime_context.py` | RuntimeContextBuilder 参数类型从 str 改为模型类型，内部调用 render() |
| `src/voidx/agent/graph/core.py` | 更新 BASE_SYSTEM_PROMPT 引用为 BASE_SYSTEM；更新 persona_prompt_for_llm 调用 |
| `src/voidx/agent/graph/subagent.py` | 同上 |
| `tests/test_agent/test_core_flow.py` | 更新导入和断言 |
