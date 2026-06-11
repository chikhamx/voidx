# Agent Role → Persona 重命名 — 技术设计文档

## Context

当前代码库中，agent 的"角色"概念使用 `role` 命名（`role_prompt`、`ROLE_PROMPTS`、`agent_role` 等），但 `role` 在 LLM 消息层已有固定含义（`system`/`user`/`assistant`/`tool`）。两个概念共用 `role` 一词造成歧义，增加阅读和沟通成本。

同时，主代理名为 `orchestrator`，但对外展示名是 `voidx`。将内部名统一为 `voidx` 可消除命名不一致。

## Goals and Non-Goals

### Goals

- 将 agent 角色相关的 `role` 命名统一改为 `persona`，消除与消息层 `role` 的歧义
- 将主代理名从 `orchestrator` 改为 `voidx`，统一内外命名
- 保持所有现有行为不变，纯重命名重构

### Non-Goals

- 不改变 agent 的行为、工具集、委派逻辑
- 不改变 prompt 内容（仅改变量名和引用）
- 不改变消息层的 `role` 字段（`user`/`assistant`/`tool` 等保持不变）
- 不改变 `compaction`/`title` 等隐藏 agent 的命名

## Architecture

重命名涉及 6 个源文件 + 3 个测试文件，按依赖关系分三层：

```
Layer 1 — 定义层（先改）
  src/voidx/agent/agents.py           ← 常量、属性、AgentDef、BUILTIN_AGENTS
  src/voidx/agent/runtime_context.py  ← RuntimeContextBuilder 参数、ContextSection name

Layer 2 — 使用层（后改）
  src/voidx/agent/graph/core.py       ← import + 调用
  src/voidx/agent/graph/subagent.py   ← import + 调用
  src/voidx/agent/tool_filters.py     ← 注释
  src/voidx/memory/context_frames.py  ← agent_role 字段

Layer 3 — 测试层（最后改）
  tests/test_agent/test_core_flow.py
  tests/test_agent/test_runtime_context.py
```

## Data Model

### 重命名映射

| 旧名 | 新名 | 作用域 |
|------|------|--------|
| `ORCHESTRATOR_PROMPT` | `VOIDX_PROMPT` | agents.py 常量 |
| `ROLE_PROMPTS` | `PERSONA_PROMPTS` | agents.py 常量 |
| `role_prompt` (property) | `persona_prompt` | AgentDef |
| `role_prompt_for_llm` | `persona_prompt_for_llm` | agents.py 函数 |
| `role_prompt` (param/attr) | `persona_prompt` | RuntimeContextBuilder |
| `"Role Prompt"` (section name) | `"Persona"` | RuntimeContextBuilder |
| `agent_role` | `agent_persona` | context_frames.py 字段 |
| `"orchestrator"` (agent name) | `"voidx"` | AgentDef.name, BUILTIN_AGENTS key |

### 不改的 `role` 用法

- 消息层 `row.role`（`"user"`/`"assistant"`/`"tool"`/`"system"`）
- `_message_role()` 函数
- LLM 消息的 `role` 字段
- `tool_filters.py` 中 `"Worker roles"` 注释 → 改为 `"Worker personas"`

## API Contract

### `AgentDef.persona_prompt` (原 `role_prompt`)

- **Signature**: `@property def persona_prompt(self) -> str`
- **Behavior**: 不变 — 查 `PERSONA_PROMPTS[self.name]`，未注册则抛 ValueError
- **Error message**: `"No persona prompt registered for agent: {self.name}"`

### `persona_prompt_for_llm` (原 `role_prompt_for_llm`)

- **Signature**: `def persona_prompt_for_llm(agent: AgentDef, *, parallel_subagents_enabled: bool = False) -> str`
- **Behavior**: 不变 — 返回 persona prompt + 子代理调度规则
- **内部检查**: `if agent.name != "voidx":` (原 `!= "orchestrator"`)

### `RuntimeContextBuilder.persona_prompt` (原 `role_prompt`)

- **Signature**: `persona_prompt: str = ""` (参数 + 属性)
- **Section name**: `"Persona"` (原 `"Role Prompt"`)

### `context_frames.py` 字段

- `agent_persona: str = "voidx"` (原 `agent_role: str = "orchestrator"`)
- SQL 列名: `agent_persona` (原 `agent_role`)
- 默认值: `"voidx"` (原 `"orchestrator"`)

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 遗漏某处旧名引用 | 全局 grep 验证，CI 测试覆盖 |
| 数据库旧数据含 `agent_role` 列 | 无迁移 — context_frames 是运行时缓存，非持久化存储，重启后重建 |
| 外部代码 import 旧名 | `agents.py` 顶层加 `__all__` 兼容别名，一个版本后移除 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 `persona` 而非 `character`/`identity` | `character` 偏游戏，`identity` 偏认证 | `persona` 在 AI 领域通用，语义最贴切 |
| 主代理名改为 `voidx` | 保持 `orchestrator` | 对外展示已是 voidx，统一减少混淆 |
| 不做数据库迁移 | 写 ALTER TABLE 迁移脚本 | context_frames 是会话级缓存，无需持久化迁移 |
| 加兼容别名 | 直接删旧名 | 避免外部插件/自定义配置立即断裂 |

## Compatibility Aliases

在 `agents.py` 顶层添加向后兼容别名，供外部代码过渡：

```python
# Deprecated — use PERSONA_PROMPTS / VOIDX_PROMPT / persona_prompt_for_llm
ROLE_PROMPTS = PERSONA_PROMPTS
ORCHESTRATOR_PROMPT = VOIDX_PROMPT
role_prompt_for_llm = persona_prompt_for_llm
```

在 `AgentDef` 上添加兼容属性：

```python
@property
def role_prompt(self) -> str:
    """Deprecated — use persona_prompt."""
    return self.persona_prompt
```

一个版本后移除所有别名。

## Open Questions

- [x] `PROMPTLESS_AGENTS` 集合名 — **保留不改**。`PERSONALESS` 语义不通，该集合表达的是"无 prompt 的 agent"，与 persona 概念无关。
- [x] `tool_contract` 中 `"Role: {self.name}"` — **改为 `"Persona: {self.name}"`**，与重命名一致。
- [x] Prompt 字符串中的 `"orchestrator"` — **保留不改**。`VOIDX_PROMPT` 开头 `"You are voidx orchestrator"` 描述的是行为角色，不是变量名。同理 `REVIEW_PROMPT` 中 `"the orchestrator should advance review"` 也是描述行为，保留。仅当代码逻辑中用字符串 `"orchestrator"` 做 agent name 匹配时才改为 `"voidx"`。
