# Persona Prompt 架构重构 — 技术设计文档

## Context

voidx 当前把 `orchestrator`、`explore`、`plan`、`implement`、`review`
都描述成"角色"，prompt 也以 "You are voidx X" 开头。这会暗示用户正在和多个身份交互，
而产品语义应该是：用户始终在和 voidx 交互，voidx 根据任务切换不同思维模式。

代码里同时用 `role` 表示 agent 侧的提示词和 LLM 消息层的
`system` / `user` / `assistant` / `tool` role。两个概念共用一个词，
会增加阅读和沟通成本。

本设计只处理 persona/prompt 命名和 runtime persona 注入，不处理 goal 与 workflow
入口重构。Goal 相关设计见 [goal-workflow-runtime.md](goal-workflow-runtime.md)，
context 注入边界见 [context-runtime-boundary.md](context-runtime-boundary.md)。

## Goals

- 将用户可感知模型从"5 个角色"改成"1 个 voidx + 多个 persona 思维模式"。
- 主循环 prompt 不再自称 `orchestrator`，而是 `You are voidx`。
- 子 agent prompt 不再自称另一个身份，而是说明 voidx 以某个 persona 工作。
- agent prompt 相关命名从 `role` 改为 `persona`，避免和消息层 `role` 混淆。
- 主 agent id 从 `orchestrator` 改为 `voidx`。
- 保持子 agent 执行架构不变：工具隔离、上下文隔离、并行委派机制保留。
- 保持用户可委派 persona 名称不变：`explore`、`plan`、`implement`、`review`。
- `compaction` 和 `title` 保持 runtime 触发，但也通过 hidden runtime persona +
  workflow context 进入同一条 HumanMessage LLM 调用链路。
- 不兼容旧内部名称，不提供 `orchestrator` / `role_prompt` 兼容别名。

## Non-Goals

- 不改变消息层 `role` 字段；`row.role`、`_message_role()`、LLM message role
  仍然表示 `system` / `user` / `assistant` / `tool`。
- 不改变用户可委派 persona 的工具集分配。
- 不在本 spec 中重构 `TaskIntent`、`Goal`、`on_intent` 或 workflow activation。
- 不迁移旧数据库；本次内部状态字段和列名直接变更，旧本地会话库不兼容。
- 不把 `compaction` / `title` 暴露为用户可委派 persona。

## Core Model

```
旧模型：多个内部角色
  orchestrator
  explore
  plan
  implement
  review

新模型：一个身份 + 多种 persona
  voidx
    explore persona      探查思维
    plan persona         设计思维
    implement persona    构建思维
    review persona       审视思维

  runtime-only hidden personas
    compaction persona   压缩上下文
    title persona        生成会话标题
```

关键区别：

- **身份**：用户始终和 voidx 对话。
- **persona**：voidx 的思维模式，用来约束关注点、输出结构和行为边界。
- **subagent**：执行隔离策略，不等同于 persona。委派时会指定 persona。
- **runtime behavior**：由 runtime 自动触发的 LLM 调用，例如 compaction/title。
  它们不是用户可见子 agent，但仍应使用 persona prompt 和 workflow context。

## Prompt Architecture

主循环：

```
BASE_SYSTEM_PROMPT
VOIDX_PROMPT
Tool Contract
Workflow Context
Task State
User Message
```

实际注入位置和字段所有权由 [context-runtime-boundary.md](context-runtime-boundary.md)
定义；本图只表达概念顺序。

子 agent：

```
BASE_SYSTEM_PROMPT
<PERSONA_PROMPT>
Tool Contract
Workflow Context
HumanMessage(task brief)
```

runtime-only behavior：

```
BASE_SYSTEM_PROMPT
COMPACTION_PERSONA / TITLE_PERSONA
Workflow Context(compaction/title)
HumanMessage(runtime task)
```

`compaction` 和 `title` 继续由 runtime 触发，不经过用户委派工具。它们仍然使用
`HumanMessage` 承载任务输入，例如 summary request 或 first user message；区别是系统前缀
由同一套 persona/workflow builder 生成，而不是散落的独立 prompt。

## Naming Changes

### Prompt Constants

| Before | After | Notes |
| --- | --- | --- |
| `ORCHESTRATOR_PROMPT` | `VOIDX_PROMPT` | 主循环 prompt |
| `EXPLORE_PROMPT` | `EXPLORE_PERSONA` | 探查 persona |
| `PLAN_PROMPT` | `PLAN_PERSONA` | 设计 persona |
| `IMPLEMENT_PROMPT` | `IMPLEMENT_PERSONA` | 构建 persona |
| `REVIEW_PROMPT` | `REVIEW_PERSONA` | 审视 persona |
| `COMPACTION_PROMPT` | `COMPACTION_PERSONA` | hidden runtime persona |
| `TITLE_PROMPT` | `TITLE_PERSONA` | hidden runtime persona |
| `ROLE_PROMPTS` | `PERSONA_PROMPTS` | prompt registry |

`PERSONA_PROMPTS` 包含：

```python
{
    "voidx": VOIDX_PROMPT,
    "explore": EXPLORE_PERSONA,
    "plan": PLAN_PERSONA,
    "implement": IMPLEMENT_PERSONA,
    "review": REVIEW_PERSONA,
    "compaction": COMPACTION_PERSONA,
    "title": TITLE_PERSONA,
}
```

`PROMPTLESS_AGENTS` 不再用于 `compaction` / `title`。如果实现后没有其它 promptless
agent，可以删除该集合。

### Built-In Agent IDs

| Before | After |
| --- | --- |
| `BUILTIN_AGENTS["orchestrator"]` | `BUILTIN_AGENTS["voidx"]` |
| `AgentDef(name="orchestrator")` | `AgentDef(name="voidx")` |

其余用户可委派 agent id 保持不变：`explore`、`plan`、`implement`、`review`。

Hidden runtime agent id 保持：`compaction`、`title`。它们不出现在 `get_subagents()` /
agent tool schema 中。

### AgentDef Semantics

`AgentDef.name` 仍然是 **agent id**，不是严格意义上的 persona 名称。原因是
`voidx`、`compaction`、`title` 都不是用户可切换的普通 persona，但它们仍需要同一套
prompt 查找和工具边界。

```python
class AgentDef(BaseModel):
    name: str           # agent id: voidx/explore/plan/implement/review/compaction/title
    description: str    # 对该 agent/persona 的描述
    when_to_use: str    # 何时使用；hidden runtime agent 写 runtime-only trigger
    tools: list[str]
    can_write: bool
    can_delegate: bool
    max_steps: int
    hidden: bool
    model: str | None
    mcp_tools: bool
```

Property rename：

```python
@property
def persona_prompt(self) -> str:
    try:
        return PERSONA_PROMPTS[self.name]
    except KeyError as exc:
        raise ValueError(f"No persona prompt registered for agent: {self.name}") from exc
```

不保留 `role_prompt` alias。遗漏引用应通过测试和 grep 暴露。

### Tool Contract

Tool contract 文案改为 persona 语义：

```text
- Persona: implement
- Can write files: true
- Can start child agents: false
- Max steps: 100
- Available tools: read, write, edit, apply_patch, ...
- Constraint: this persona must not start another child agent.
```

对 hidden runtime persona，tool list 通常为空，且不暴露给 agent tool。

### RuntimeContextBuilder

| Before | After |
| --- | --- |
| `role_prompt: str = ""` | `persona_prompt: str = ""` |
| `self.role_prompt` | `self.persona_prompt` |
| `ContextSection(name="Role Prompt")` | `ContextSection(name="Persona")` |

`BASE_SYSTEM_PROMPT` 中的措辞同步改：

- `Do not expose internal role names` -> `Do not expose internal persona names`
- `over role prompts, delegation rules` -> `over persona prompts, delegation rules`

## Prompt Content

### VOIDX_PROMPT

主 prompt 应表达 voidx 是主协调者，但避免 `orchestrator` 这个身份词：

```python
VOIDX_PROMPT = """You are voidx.

## Thinking Style

- Assess before acting.
- Stay aligned with the user's actual goal.
- Switch persona when a different thinking style is useful.
- Delegate only when the task is broad, risky, or benefits from isolated context.
- Coordinate the work without exposing internal persona names to the user.

## Responsibilities

- Judge current state.
- Judge the next step.
- Judge whether a persona switch or subagent delegation is useful.
- Judge completion only after verification evidence exists.

## Rules

- Do not delegate to implement persona unless the user explicitly asks to modify code.
- Subagents do not interact with the user.
- Runtime workflow gates take precedence over persona prompts and delegation rules.
"""
```

### User-Visible Personas

User-visible persona prompts should start with thinking mode, not identity:

```python
EXPLORE_PERSONA = """## Persona: explore

Use this persona for evidence gathering and codebase exploration.
Search broadly before narrowing. Report findings with concrete paths and lines.
Do not suggest edits unless explicitly asked.
"""
```

The same pattern applies to `plan`, `implement`, and `review`.

### Runtime-Only Personas

Runtime-only persona prompts are hidden and task-specific:

```python
COMPACTION_PERSONA = """## Persona: compaction

Summarize conversation history for continuation. Preserve durable facts,
decisions, constraints, open work, and final tool outcomes. Do not narrate
step-by-step execution.
"""

TITLE_PERSONA = """## Persona: title

Generate a short session title from the first user message. Output only the
title text. No quotes, markdown, or explanation.
"""
```

## Runtime Workflows for Hidden Personas

Add workflow nodes for runtime-only behaviors:

- `compaction`: selected only by runtime trigger. It defines summary quality,
  fallback expectations, and what information must survive compression.
- `title`: selected only by runtime trigger. It defines title constraints and
  stale-task safety rules.

These nodes are not selected from user text and are not shown as user-facing workflow suggestions.
The runtime passes them explicitly, e.g. `workflow_context_for(..., runtime_trigger="compaction")`.

Compaction/title LLM calls should save context frames with `agent_persona="compaction"` or
`agent_persona="title"` so debugging shows the same terminology as main turns and subagents.

## Data and Persistence

### Context Frames

Rename `agent_role` to `agent_persona` everywhere:

- `ContextFrameRecord.agent_role` -> `ContextFrameRecord.agent_persona`
- `build_context_frame(agent_role=...)` -> `build_context_frame(agent_persona=...)`
- `save_context_frame_from_messages(agent_role=...)` -> `agent_persona=...`
- SQL column `context_frames.agent_role` -> `context_frames.agent_persona`
- Default value `orchestrator` -> `voidx`

No compatibility migration is provided. Existing local SQLite databases with the old column are
unsupported after this change and must be cleared.

### Runtime State

Any serialized top-level agent value changes from `orchestrator` to `voidx`.
Old snapshots that contain `orchestrator` are unsupported.

## Implementation Order

1. Rename prompt constants and `ROLE_PROMPTS` registry.
2. Rename `AgentDef.role_prompt` to `persona_prompt`.
3. Rename main agent id from `orchestrator` to `voidx`.
4. Update `RuntimeContextBuilder` parameter and section names.
5. Update graph/subagent/title/compaction call sites.
6. Update context frame field and SQL column.
7. Update UI display labels and event payload metadata from role wording to persona wording.
8. Update tests and remove compatibility expectations.

## Files Expected to Change

- `src/voidx/agent/agents.py`
- `src/voidx/agent/runtime_context.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/subagent.py`
- `src/voidx/agent/graph/turn_runner.py`
- `src/voidx/agent/graph/compaction_coordinator.py`
- `src/voidx/agent/graph/session_runtime.py`
- `src/voidx/memory/context_frames.py`
- `src/voidx/memory/store.py`
- `src/voidx/ui/output/agent_display.py`
- Tests referencing `orchestrator`, `role_prompt`, `ROLE_PROMPTS`, or `agent_role`

## Testing

- Unit tests for `persona_prompt` lookup and missing prompt errors.
- Unit tests that `get_agent("voidx")` exists and `get_agent("orchestrator")` does not.
- Unit tests that `get_subagents()` excludes `voidx`, `compaction`, and `title`.
- Runtime context tests asserting `## Persona` replaces `## Role Prompt`.
- Context frame tests asserting `agent_persona` is written for main, subagent, compaction, and title calls.
- Prompt tests ensuring user-visible prompts do not say `You are voidx explore/plan/...`.

## Decisions

| Decision | Alternative | Reason |
| --- | --- | --- |
| Use `persona` for prompt/mode wording | `role`, `character`, `identity` | Avoids message role collision and fits AI prompt terminology |
| Keep `AgentDef.name` as agent id | Rename to `persona` | Hidden runtime agents and primary `voidx` are not all user personas |
| Rename `orchestrator` to `voidx` | Keep internal name | Internal and external naming should match |
| No compatibility aliases | Keep `role_prompt`/`orchestrator` aliases | This is an internal breaking cleanup; aliases hide missed call sites |
| Compaction/title get hidden runtime personas | Keep separate standalone prompts | Same LLM call stack, workflow injection, and context-frame terminology should apply consistently |
