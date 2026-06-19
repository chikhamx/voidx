> **Status: Done**

# 拆分 agent/graph/core.py — 技术设计文档

## Context

`core.py` 是 `agent/graph/` 中最大的文件（932 行），包含 `VoidXGraph` 类（670 行）和 12 个顶层辅助函数。`VoidXGraph` 是一个 God Class，混合了初始化、LLM 调用、子 agent 调度、session 管理、guidance 等多种职责。这使得：
- 难以快速定位某个逻辑的归属
- 修改 LLM 调用逻辑时需要在 932 行中翻找
- `_call_llm` 单个方法就有 174 行，包含重试、compaction、token 统计等子流程

## Goals and Non-Goals

### Goals

- 将 `core.py` 拆为子包 `core/`，每个模块 < 350 行
- 保持 `from voidx.agent.graph import VoidXGraph` 导入路径不变
- 保持 `VoidXGraph` 的 mixin 继承结构不变
- 运行时行为零改变

### Non-Goals

- 不重构 `VoidXGraph` 的 mixin 体系（那是另一个话题）
- 不拆 `streaming.py`、`compaction_coordinator.py` 等其他大文件
- 不改变公开 API 签名

## Architecture

### 当前结构

```
core.py (932 行)
├── 12 个顶层辅助函数 (L109-L257, ~150 行)
│   ├── _is_context_overflow_error
│   ├── _render_inline_compaction_guide
│   ├── _merge_workflow_runs, _workflow_names
│   ├── _persona_for_workflow_runs, _persona_for_child_workflow
│   ├── _interaction_mode_for_persona, _subagent_step_budget
│   ├── _invalidate_tui
│   ├── _agent_static_tool_defs
│   └── _task_state_for_context
│
└── class VoidXGraph (L259-L932, ~670 行)
    ├── __init__ + 属性 (L268-L399, ~130 行)
    ├── guidance 管理 (L410-L431, ~20 行)
    ├── session 代理方法 (L433-L519, ~90 行)
    ├── _subagent_runner (L521-L641, ~120 行)
    ├── _build + _prepare_with_stream (L648-L718, ~70 行)
    ├── _inline_compaction_guide_for (L723-L745, ~25 行)
    ├── _call_llm (L747-L917, ~170 行)
    └── _router + _finalize (L919-L932, ~15 行)
```

### 目标结构

```
agent/graph/core/
├── __init__.py         # re-export VoidXGraph
├── helpers.py          # 顶层辅助函数 (~150 行)
├── session.py          # session 代理方法 + clear/resume (~110 行)
├── subagent.py         # _subagent_runner (~120 行)
├── llm.py              # _call_llm + _prepare_with_stream + _router + _finalize (~280 行)
└── _voidx_graph.py     # VoidXGraph 类（精简后 ~200 行）
```

### 模块职责

#### `helpers.py` (~150 行)

从 `core.py` 提取的纯函数，无 `VoidXGraph` 依赖：

| 函数 | 行数 | 备注 |
|------|------|------|
| `_is_context_overflow_error` | 16 | |
| `_render_inline_compaction_guide` | 18 | |
| `_merge_workflow_runs` | 10 | |
| `_workflow_names` | 12 | |
| `_persona_for_workflow_runs` | 17 | |
| `_persona_for_child_workflow` | 6 | |
| `_interaction_mode_for_persona` | 3 | |
| `_subagent_step_budget` | 12 | |
| `_invalidate_tui` | 5 | |
| `_agent_static_tool_defs` | 19 | |
| `_task_state_for_context` | 12 | |

**依赖**：`voidx.workflow.types`、`voidx.agent.agents`、`voidx.agent.task_state`、`voidx.agent.runtime_context`、`voidx.workflow`。无内部模块依赖。

#### `session.py` (~110 行)

从 `VoidXGraph` 提取的 session 管理方法，作为 mixin：

```python
class GraphSessionMixin:
    async def persist_runtime_state(self): ...
    async def compact_session_history(self, *, force=True): ...
    async def restore_transcript_snapshot(self, *, append=False): ...
    async def show_startup(self, *, append_transcript=False, prefer_direct=False): ...
    async def run_synthetic_turn(self, text, *, display_text=None): ...
    async def clear_current_session(self): ...
    def _schedule_clear_session_storage(self, session_id): ...
    async def _clear_session_storage(self, session_id): ...
    def _reload_parallel_subagents_from_settings(self): ...
    async def resume_session(self, session): ...
    async def set_session_title(self, title): ...
```

**注意**：这些方法目前是 `VoidXGraph` 上的直接方法，大部分是代理到 mixin 的薄包装（`await self._xxx()`）。提取后仍需挂在 `VoidXGraph` 上才能被外部调用。

**决策**：这些方法太薄（多数只有 1-2 行），且都是 `self._xxx()` 的代理。提取为独立 mixin 增加了间接层但收益极小。**不提取**，留在 `_voidx_graph.py` 中。

#### `subagent.py` (~120 行)

`_subagent_runner` 方法提取为 mixin：

```python
class GraphSubagentMixin:
    async def _subagent_runner(self, agent_def, description, model_override, goal_resolution, result_contract): ...
```

**问题**：`_subagent_runner` 大量访问 `self` 属性（`self._session`、`self._ui`、`self._next_agent_id` 等），提取为 mixin 与留在类中无本质区别。且它只被 `__init__` 中作为回调传入 `build_tool_registry`，不是 graph 节点。

**决策**：`_subagent_runner` 是 `VoidXGraph` 的核心方法之一，与初始化紧密耦合。提取为独立文件意义不大。**不提取**。

#### `llm.py` (~280 行)

graph 节点方法提取为 mixin：

```python
class GraphLlmMixin:
    async def _prepare_with_stream(self, state): ...   # ~70 行
    async def _call_llm(self, state): ...              # ~170 行
    def _router(self, state): ...                      # ~5 行
    async def _finalize(self, state): ...              # ~15 行
    async def _workflow_context_for(self, *args, **kwargs): ...  # ~2 行
    def _inline_compaction_guide_for(self, messages): ...        # ~25 行
```

**依赖**：`self.model`、`self.tools`、`self._ui`、`self._session`、`self.config`、`self._compaction`、`self._usage_stats`、`self._debug`、`self._pending_summary`、`self._compaction_summary`、`self._context_cache`、`self._task_state`、`self._instruction`、`self._session_date`、`self._drain_pending_guidance`、`self._in_turn_compact`。

**问题**：与 `_subagent_runner` 一样，这些方法深度依赖 `self`。但它们是 LangGraph 的节点函数，职责清晰（LLM 调用 + 上下文准备），且 `_call_llm` 有 170 行，是 `core.py` 膨胀的主因。

**决策**：这是唯一值得提取的部分。提取为 mixin 可以将 `core.py` 的 `VoidXGraph` 类从 670 行降到 ~350 行。

### 修正后的目标结构

```
agent/graph/core/
├── __init__.py         # re-export VoidXGraph
├── helpers.py          # 11 个顶层辅助函数 (~150 行)
├── llm.py              # GraphLlmMixin: _call_llm + _prepare_with_stream + _router + _finalize (~280 行)
└── _voidx_graph.py     # VoidXGraph 类（精简后 ~500 行）
```

`VoidXGraph` 继承链变为：

```python
class VoidXGraph(
    GraphTitleMixin,
    GraphRunLoopMixin,
    GraphCompactionMixin,
    GraphToolExecutionMixin,
    GraphPermissionMixin,
    GraphLlmMixin,       # 新增
):
```

### 导入依赖

```
core/helpers.py ← (无内部依赖)
core/llm.py ← core/helpers
core/_voidx_graph.py ← core/helpers, core/llm
core/__init__.py ← core/_voidx_graph
```

无循环依赖。

### `__init__.py` re-export

```python
"""Agent graph — LangGraph state machine with 5-agent system."""

from voidx.agent.graph.core._voidx_graph import VoidXGraph

__all__ = ["VoidXGraph"]
```

保持 `from voidx.agent.graph import VoidXGraph` 和 `from voidx.agent.graph.core import VoidXGraph` 均有效。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 只提取 helpers + LLM mixin | 拆为 5-6 个子模块 | session/subagent 方法太薄或 self 耦合太深，提取收益 < 复杂度 |
| 用 mixin 而非独立函数 | 把 _call_llm 提取为独立函数 | _call_llm 深度依赖 self 状态，独立函数需要传 15+ 参数 |
| 保留 `_voidx_graph.py` 而非 `graph.py` | `graph.py` | 避免与 `agent/graph/` 目录名混淆 |
| `helpers.py` 不加下划线前缀 | `_helpers.py` | 与 tool_executor 拆分保持一致，包内相对导入已表达内部性 |

## Open Questions

- `_call_llm` 内部的 `rebuild_llm_messages` 和 `save_context_frame` 闭包是否也值得提取？目前它们只在 `_call_llm` 内使用，提取为 helpers 会增加参数传递。建议暂不提取。
