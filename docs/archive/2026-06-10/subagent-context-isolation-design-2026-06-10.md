# Subagent Result and State Management — 技术设计文档

> **Status: Done**

## Context

子 agent 的上下文隔离语义已经是当前实现的基线：

- `run_subagent` 只从 `task_description` 建立子 agent 消息上下文，见 `src/voidx/agent/graph/subagent.py:44-88`。
- `agent` 工具描述已经要求调用方提供完整、自包含的任务描述，见 `src/voidx/tools/agent.py:20-41`。
- 子 agent 的中间 `AIMessage` / `ToolMessage` 只保存在 `sub_messages`、UI capture 和 worker context frame 中；主 agent 通过 `agent` 工具对应的 `ToolMessage` 接收最终文本结果。

这份文档不再设计上下文隔离本身，而是聚焦隔离完成后的两个真实缺口：

1. 子 agent 只返回纯文本，结构化执行信息丢失。
2. 子 agent 使用独立工具执行 loop，状态管理能力弱于主 agent。

## Current Behavior

### 问题 1：子 agent 最终结果缺少结构化信息

`run_subagent` 当前返回 `str`，主要返回点在 `src/voidx/agent/graph/subagent.py:189-215` 和 `src/voidx/agent/graph/subagent.py:258-260`。

`AgentTool.execute` 将该字符串直接写入 `ToolResult.output`，只附带 `agent` 和 `model` metadata，见 `src/voidx/tools/agent.py:96-104`。

后果：

- 主 agent 能看到子 agent 最终文本，但看不到结构化的执行摘要。
- 子 agent 修改过哪些文件、用了哪些工具、执行了多少步，只能靠自然语言总结或 UI/transcript 调试信息推断。
- 后续如果主 agent 需要基于子 agent 结果做状态推进、风险判断或精确摘要，缺少稳定字段。

### 问题 2：子 agent 工具执行路径缺少主 agent 的状态能力

主 agent 的工具执行由 `GraphToolExecutor` 统一处理，包含：

| 能力 | 主 agent 位置 | 子 agent 当前状态 |
|------|---------------|------------------|
| 完整 `ToolContext` | `tool_executor.py:83-101` | `subagent.py:116-121` 只设置 workspace / lsp / sandbox |
| on-failure 通知 | `tool_executor.py:179-185` | 缺失 |
| todo UI 事件 | `tool_executor.py:187-190` | 已有：`subagent.py:235-238` |
| diff 记录与 UI 输出 | `tool_executor.py:204-222` | 缺失 |
| state patch / workflow runs 合并 | `tool_executor.py:388-432` | 缺失 |
| context overflow compaction | `compaction_coordinator.py:88-153` | 缺失 |

子 agent 不经过 LangGraph 工具执行节点，而是在 `run_subagent` 内部直接调用 `agent_tools.execute_tool()`，见 `src/voidx/agent/graph/subagent.py:228-245`。这条独立路径是合理的，但需要补齐必要状态。

## Goals

- 保持现有隔离语义：子 agent 仍只接收自包含 task description。
- 保持主 agent 上下文精简：仍只接收 `agent` 工具的最终 `ToolMessage`，不注入子 agent 中间消息。
- 为子 agent 结果增加结构化字段，至少包含最终文本、执行步数、工具统计、变更文件、状态 patch 摘要。
- 补齐子 agent 工具执行 loop 中最影响正确性的状态能力：完整 `ToolContext`、diff 记录、state patch 聚合、轻量 compaction。
- 保留现有 UI capture、worker context frame、usage stats 和 todo event 行为。

## Non-Goals

- 不重新引入调用方历史继承。
- 不把子 agent 的中间消息追加回主 agent 的 LangGraph messages。
- 不把子 agent 完整改造成 `GraphToolExecutor` 调用方；该重构可以作为远期方案。
- 不改变 `agent` 工具向 LLM 暴露的主要输出形式：`ToolMessage.content` 仍是最终文本。

## Architecture

### 设计项 A：新增 `SubagentResult`

**文件**: `src/voidx/agent/graph/subagent.py`

新增 Pydantic model，作为 `run_subagent` 的标准返回值：

```python
class SubagentResult(BaseModel):
    output: str
    changed_files: list[str] = Field(default_factory=list)
    step_count: int = 0
    max_steps: int = 0
    tool_summary: dict[str, int] = Field(default_factory=dict)
    state_patch: dict = Field(default_factory=dict)
```

字段含义：

- `output`: 子 agent 最终文本结果，保持现有行为。
- `changed_files`: 子 agent 工具结果中出现 diff 的文件路径列表。
- `step_count`: 实际执行到的 step。
- `max_steps`: agent 配置的最大 step。
- `tool_summary`: 工具调用次数统计，例如 `{"read": 3, "edit": 1}`。
- `state_patch`: 子 agent 工具结果产生的状态更新摘要，初期只放已验证能安全回流的字段。

`run_subagent` 的返回类型改为：

```python
) -> SubagentResult:
```

所有现有返回点统一走 helper：

```python
def finish(output: str, *, step: int) -> SubagentResult:
    return SubagentResult(
        output=output,
        changed_files=sorted(changed_files),
        step_count=step,
        max_steps=agent_def.max_steps,
        tool_summary=dict(sorted(tool_counts.items())),
        state_patch=state_patch,
    )
```

### 设计项 B：`AgentTool` 拆包结构化结果

**文件**: `src/voidx/tools/agent.py`

`AgentTool.execute` 负责兼容标准返回和测试 mock 返回：

```python
raw = await self._run_child_agent(agent_def, inp.description, inp.model)
if isinstance(raw, SubagentResult):
    output = raw.output
    metadata = {
        "agent": agent_name,
        "model": inp.model or getattr(agent_def, "model", None) or "default",
        "changed_files": raw.changed_files,
        "step_count": raw.step_count,
        "max_steps": raw.max_steps,
        "tool_summary": raw.tool_summary,
        "state_patch": raw.state_patch,
    }
else:
    output = str(raw)
    metadata = {
        "agent": agent_name,
        "model": inp.model or getattr(agent_def, "model", None) or "default",
    }
```

`ToolResult.output` 仍只放 `output`，保证主 agent 的可读上下文不膨胀；结构化字段放入 `ToolResult.metadata`，供运行时和后续逻辑使用。

### 设计项 C：子 agent 工具执行收集结构化状态

**文件**: `src/voidx/agent/graph/subagent.py`

在 `run_subagent` loop 外初始化：

```python
changed_files: set[str] = set()
tool_counts: dict[str, int] = {}
state_patch: dict = {}
```

在 `run_one` 中：

- 每次工具执行后递增 `tool_counts[tid]`。
- 如果 `ToolResult.diff` 存在，记录 diff 到 UI/session tracker，并从 diff header 提取文件路径写入 `changed_files`。
- 如果 `ToolResult.metadata` 含 `state_patch` 或 `on_intent.state_patch`，复用主 agent 的状态解析逻辑合并到 `state_patch`。
- `todo` 事件保持现状。

状态解析应优先复用 `tool_executor.py` 中已有的 helper，而不是复制复杂逻辑。可选做法：

- 将 `_state_update_from_executed_tools` 拆成接受轻量结构的公共 helper；或
- 新增 `collect_tool_state_patch(result, current_skill_runs=...)`，由主 agent 和子 agent 共用。

### 设计项 D：补齐子 agent `ToolContext`

**文件**: `src/voidx/agent/graph/subagent.py`

当前子 agent `ToolContext` 只包含 workspace / lsp / sandbox。需要补齐：

- `session_id`
- `agent`
- `interaction_mode`
- `task_intent`
- `active_skill_names`
- `skill_runs`
- `mcp_manager`（如果可从调用方传入）
- `file_mtimes`（如果可从调用方传入）

其中 `mcp_manager` 和 `file_mtimes` 需要由 `_subagent_runner` 透传。若暂时不透传，先保持为空，但接口设计应预留参数，避免后续再改签名。

### 设计项 E：轻量 compaction

**文件**: `src/voidx/agent/graph/subagent.py`

子 agent 每步调用前已经估算 context tokens，见 `subagent.py:147-152`。在该位置增加 overflow 检查：

- 如果未超过预算，不做任何事。
- 如果超过预算，对较早的子 agent 消息做摘要，保留：
  - 初始 task description
  - runtime / workflow context overlay
  - 最近一轮 AI/tool adjacency
  - 已收集的 `changed_files`、`tool_summary`、`state_patch`
- 摘要失败时使用 `generate_fallback_summary`，并继续执行，不让 compaction 失败直接中断子 agent。

这应是子 agent 内部 compaction，不持久化为主会话 compaction。worker context frame 仍保存实际传给 LLM 的 messages。

### 设计项 F：on-failure 通知

on-failure 通知依赖主 graph host 的 `_needs_failure_check` 和 permission policy。子 agent 当前只有 `authorize_tools` 回调，不直接持有 host。建议分两步：

1. 先记录失败工具到 `SubagentResult.state_patch` 或 metadata，供 orchestrator 可见。
2. 后续如需要交互式 on-failure 提示，再把主 agent 的 failure notification 抽成可注入 callback。

不建议第一阶段让 `run_subagent` 直接依赖 graph host。

## Data Model

新增：

```python
class SubagentResult(BaseModel):
    output: str
    changed_files: list[str] = Field(default_factory=list)
    step_count: int = 0
    max_steps: int = 0
    tool_summary: dict[str, int] = Field(default_factory=dict)
    state_patch: dict = Field(default_factory=dict)
```

`state_patch` 初期保持 `dict`，避免 prematurely exposing a broad public model。写入前必须经过现有 `ToolStatePatch` 校验。

## API Contract

### `run_subagent`

Before:

```python
async def run_subagent(...) -> str:
```

After:

```python
async def run_subagent(...) -> SubagentResult:
```

### `_subagent_runner`

`_subagent_runner` 返回 `SubagentResult`，但不用拆包。它继续负责：

- 创建子 agent workflow context
- 注入 `sub_messages` 用于 UI / transcript
- 发送 `SubagentStarted` / `SubagentFinished`

### `AgentTool.execute`

`AgentTool.execute` 对调用方保持兼容：

- `ToolResult.output`: 子 agent 最终文本
- `ToolResult.metadata`: 结构化字段
- 非标准返回值：转成字符串，metadata 只保留 agent/model

## Error Handling

| 场景 | 策略 |
|------|------|
| 子 agent 正常结束 | 返回 `SubagentResult(output=...)` |
| 子 agent 达到 max steps | 返回 fallback output，并带上已收集的结构化字段 |
| 子 agent compaction 失败 | 使用 fallback summary，保留结构化字段，继续执行 |
| 工具返回非法 state patch | 忽略该 patch，记录到 debug/metadata，不能污染 runtime state |
| `AgentTool` 收到旧 mock 字符串 | 兼容为纯文本 ToolResult |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 使用 `SubagentResult` | 继续只返回纯文本 | 结构化信息不应依赖自然语言总结 |
| `ToolResult.output` 保持纯文本 | 把 JSON 混进 output | 避免污染主 agent LLM 上下文；结构化字段放 metadata |
| 子 agent loop 渐进补齐状态 | 直接复用 `GraphToolExecutor` | 复用需要较大重构，当前缺口可以局部补齐 |
| on-failure 分阶段处理 | 第一阶段直接依赖 graph host | 保持子 agent loop 可测试、低耦合 |
| `state_patch` 先用 dict | 立刻新增广义 public model | 现有 `ToolStatePatch` 已负责校验，先减少 API 面 |

## Test Plan

### 结构化返回

- `run_subagent` 返回 `SubagentResult`。
- 无工具调用时，`output` 与旧字符串返回一致。
- 达到 max steps fallback 时，仍返回 `SubagentResult`。
- `step_count`、`max_steps`、`tool_summary` 正确。

### `AgentTool` 兼容

- 标准 `SubagentResult` 返回会拆成 `ToolResult.output` + metadata。
- mock runner 返回字符串时仍兼容现有测试。
- `agent` 工具的 `ToolMessage.content` 仍只包含最终文本。

### diff / changed files

- 子 agent 执行带 diff 的 edit/write/apply_patch 工具后，`changed_files` 包含对应路径。
- diff 记录不会把完整 diff 注入主 agent messages。
- UI capture 和 transcript 仍能看到子 agent 工具步骤。

### state patch

- 子 agent 工具返回合法 `state_patch` 时，`SubagentResult.state_patch` 包含校验后的字段。
- 非法 patch 被忽略或记录为非阻塞错误。
- workflow runs 字段使用当前 `WorkflowRunState`，不再使用旧 skill run 类型。

### compaction

- 构造超预算子 agent messages，验证会生成摘要并继续执行。
- compaction 后 `changed_files` / `tool_summary` / `state_patch` 不丢失。
- worker context frame 保存 compaction 后实际发送给 LLM 的 messages。

### 建议验证命令

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -k "subagent or child_agent or parallel" -v
```

如果实现触及 shared tool-state helper，再运行：

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_agent/test_run_loop.py tests/test_tools/test_basic.py -v
```

## Open Questions

- `SubagentResult.state_patch` 是否应由 `AgentTool.execute` 自动应用到 orchestrator state，还是只作为 metadata 暴露给后续节点？
- `SubagentResult` 是否需要包含 todo 状态快照？当前 todo 更新主要面向 UI，主 agent LLM 不一定能看到最终 todo 列表。
- 子 agent compaction 是否需要复用主 agent 的 LLM compaction agent，还是只使用轻量 fallback summary？
