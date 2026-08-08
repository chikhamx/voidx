# 子 Agent 结构化汇报协议设计

> **Status: Design**
> Date: 2026-07-13

## Context

当前父 agent 与子 agent 的通信主要依赖子 agent 最后一段自然语言文本：

```text
run_subagent()
  -> returns str
AgentTool.execute()
  -> ToolResult(output=str, metadata={agent, result_schema, ...})
workflow auto-advance
  -> regex parses ToolResult.output for verdict: FAIL|NEEDS_CHANGE
UI
  -> renders a bounded preview for agent tool output
parent LLM
  -> receives full ToolMessage only if graph continues to call_llm
```

这个模型在普通 inspect/plan 场景下可以工作，但在 review workflow 中暴露出协议边界问题：

1. review 子 agent 返回 `verdict: FAIL` 或 `NEEDS_CHANGE`。
2. `auto_advance_events()` 从文本中正则识别 `review_has_issues`。
3. workflow 状态推进到 `feedback`，并设置 `should_continue=False`，避免主 agent 自动继续修复。
4. graph 直接进入 `finalize`，不再回到主 LLM。
5. `_finalize()` 只有 `convergence_forced` 时才补 fallback summary，普通终止不生成最终 assistant message。
6. UI 对 agent tool 只展示 preview，用户看不到完整 review 结论。

结果是：子 agent 已经完成并产出有用结果，但主任务对用户表现为"没有汇报结果"或"卡住"。

## Problem

根因不是单一 UI bug，而是父子 agent 协议过度文本化，导致四类语义混在一起：

| 语义 | 当前承载方式 | 问题 |
| --- | --- | --- |
| 子 agent 是否完整完成 | `finish_reason` + 文本 | 与工具执行 success/error 混淆 |
| review verdict | `ToolResult.output` 文本正则 | 依赖格式稳定，无法表达结构化 findings |
| 用户可见报告 | agent tool preview 或主 LLM 总结 | 当 graph 停止后没有兜底出口 |
| workflow 生命周期 | `should_continue` bool | 同时表达"停止执行"和"本轮已有最终答复" |

现有 `docs/specs/subagent-incomplete-result-status-2026-07-11.md` 已处理"子 agent 结果完整性状态"。本设计补上另一半：子 agent 如何以机器可读、用户可见、可驱动 workflow 的方式汇报结果。

## Goals

- 为子 agent 增加结构化终止汇报协议，避免父层从自然语言中解析关键控制信号。
- 让 review/inspect/plan/implement/feedback 等结果 contract 有明确 schema。
- 让 workflow auto-advance 优先消费结构化 metadata，而不是正则扫文本。
- 当 workflow 因 review 结果终止本轮时，仍能生成确定性的用户可见最终报告。
- 保持安全边界：review 发现问题后仍不自动进入修复工具调用。
- 兼容迁移：旧文本 contract 和 regex fallback 在过渡期仍可工作。

## Non-Goals

- 不在本设计中重做整个 workflow DAG。
- 不改变普通工具的 ToolMessage success/error 语义。
- 不把子 agent 汇报做成流式中间状态协议；本设计只定义终止汇报。
- 不要求一次性迁移所有子 agent mode；review 优先。
- 不解决完整 transcript 分页查看问题；长报告展示仍可独立优化。

## Proposed Design

### 1. 增加子 agent 专用终止工具

给子 agent 运行环境注入一个专用工具，暂名 `agent_report`。它只在子 agent 内可见，父 agent 不直接使用。

#### 1.1 注入机制：synthetic tool call 拦截

> **决策**：`agent_report` 不作为真实 `BaseTool` 注册到 `ToolRegistry`，而是由 `run_subagent()` 在 tool dispatch 层拦截。

理由：
- `run_subagent()` 当前从父 agent 继承 `parent_tools`（`subagent.py:91`），只屏蔽 `{"agent", "clarify", "checkpoint"}`。如果 `agent_report` 注册为真实 `BaseTool`，需要额外逻辑确保它不泄漏到父 registry，增加耦合。
- `agent_report` 是终止出口而非业务工具，不需要走 `ToolRegistry.execute_tool()` 的正常 dispatch 路径（权限检查、tracker、session event 等）。
- 拦截方式更简单：在 `run_subagent()` 的 `run_one()` 函数（`subagent.py:333-370`）中，检查 `tc.get("name") == "agent_report"`，如果匹配则直接构造 `SubagentRunResult` 并终止 loop，不调用 `agent_tools.execute_tool()`。

实现方式：`agent_report` 的 JSON schema 通过 `model_to_json_schema(AgentReportInput)` 生成，包装为与 `ToolRegistry.tools_for_llm()` 相同的格式（`{"type": "function", "function": {"name": "agent_report", "description": ..., "parameters": ..., "strict": True}}`，见 `registry.py:131-148`），追加到 `tool_defs` 列表中。子 agent LLM 通过 `model.bind_tools(tool_defs)` 看到它是一个可用工具，但实际执行被 `run_subagent()` 拦截。

```python
# 在 run_subagent() 中，构建 tool_defs 时注入 agent_report schema
tool_defs = agent_tools.tools_for_llm()
tool_defs = filter_unavailable_lsp_tools(tool_defs, lsp_manager)
tool_defs = strip_gemini_unsupported_schema_keys(tool_defs, resolve_protocol(config.model))
# 新增：注入 agent_report 的 schema（仅 schema，不含实际 BaseTool）
tool_defs = [*tool_defs, _agent_report_tool_def(result_contract)]
```

在 `run_one()` 中拦截：
```python
async def run_one(tc):
    tid = tc.get("name", "")
    if tid == "agent_report":
        # 拦截：解析参数，构造 SubagentRunResult，终止 loop
        report_input = AgentReportInput.model_validate(tc.get("args", {}))
        raise _AgentReportSignal(report_input)  # 通过异常跳出 loop
    # ... 正常 tool dispatch ...
```

`_AgentReportSignal` 是内部异常，在 `run_subagent()` 的 `try` 块内（`subagent.py:176`）被单独捕获，构造 `SubagentRunResult` 并返回。注意：`run_one()` 在 `asyncio.gather()`（`subagent.py:372`）中执行，异常会从 gather 传播到外层 `try`。必须在通用 `except Exception`（`subagent.py:443`）**之前**用 `except _AgentReportSignal` 捕获，否则会被当作 `error` 路径处理。捕获后跳过后续 tool message 拼接和 guard 检查，直接返回。

`agent_report` 是终止出口，不是普通业务工具：

- 子 agent 调用后，`run_subagent()` 立即结束。
- 调用后不能再读文件、执行 shell 或调用其他工具。
- run loop 将工具参数保存为结构化结果，并返回给 `AgentTool`。

建议输入 schema：

```python
class AgentReportFinding(BaseModel):
    severity: Literal["high", "medium", "low", "info"] = "info"
    title: str
    evidence: str = ""
    recommendation: str = ""


class AgentReportInput(BaseModel):
    schema_name: str
    status: Literal["complete", "incomplete", "failed"] = "complete"
    verdict: Literal["PASS", "FAIL", "NEEDS_CHANGE", "UNKNOWN"] = "UNKNOWN"
    summary: str
    findings: list[AgentReportFinding] = []
    risks: list[str] = []
    next_actions: list[str] = []
    verification_notes: list[str] = []
    user_visible_report: str = ""
```

`schema_name` 对齐现有 `AgentResultContract.schema_name`，例如：

| Mode | schema_name | 关键字段 |
| --- | --- | --- |
| `review` | `review_result` | `verdict`, `findings`, `risks`, `next_actions` |
| `inspect` | `inspection_result` | `summary`, `findings`, `open_questions` |
| `plan` | `plan_result` | `summary`, `next_actions`, `risks` |
| `implement` | `implementation_result` | `status`, `verification_notes`, `next_actions` |
| `feedback` | `feedback_result` | `status`, `findings`, `verification_notes` |

V1 可以使用一个通用 schema 承载所有 mode，后续再按 mode 拆更严格的 discriminated union。

> **`AgentReportInput` 与 `AgentStructuredReport` 的关系**：`AgentReportInput` 是子 agent 通过工具调用提交的输入；`AgentStructuredReport` 是 `run_subagent()` 返回给 `AgentTool` 的输出。两者字段几乎相同，因为 `AgentStructuredReport` 在 `agent_report` 路径上直接由 `AgentReportInput` 映射而来。`AgentStructuredReport` 额外承载 legacy 合成路径的结果（此时不经过 `AgentReportInput`）。后续如果需要在输出侧补充合成字段（如 `rendered_text`），两个类型可以独立演化。

### 2. `run_subagent()` 返回结构化结果

> **当前状态**：`run_subagent()` 返回 `str`（`src/voidx/agent/graph/subagent.py:81`）。`SubagentRunResult` 是本设计**新建**的类型，不是已有类型的扩展。

新建 `SubagentRunResult`，承载结构化终止结果。`run_subagent()` 的返回类型从 `str` 改为 `SubagentRunResult`：

```python
class AgentStructuredReport(BaseModel):
    schema_name: str
    status: Literal["complete", "incomplete", "failed"]
    verdict: Literal["PASS", "FAIL", "NEEDS_CHANGE", "UNKNOWN"] = "UNKNOWN"
    summary: str
    findings: list[AgentReportFinding] = []
    risks: list[str] = []
    next_actions: list[str] = []
    verification_notes: list[str] = []
    user_visible_report: str = ""


class SubagentRunResult(BaseModel):
    output: str
    finish_reason: str
    status: Literal["complete", "incomplete", "failed"]
    complete: bool
    contract_satisfied: bool | None = None
    report: AgentStructuredReport | None = None
```

`output` 仍保留，作为人类可读报告和兼容旧调用方的主要文本。若 `agent_report.user_visible_report` 非空，则 `output = user_visible_report`；否则由 report 字段确定性渲染一份文本。

`finish_reason` 和 `status` 的来源见 §7「终止路径映射」。`status` / `complete` / `contract_satisfied` 字段与 `docs/specs/subagent-incomplete-result-status-2026-07-11.md` 定义的状态语义对齐，两个设计在此合并为统一类型。

#### 受影响调用方

| 调用方 | 当前代码位置 | 需要的变更 |
| --- | --- | --- |
| `AgentTool.execute()` | `src/voidx/agent/adapters/tools/subagent.py` | `output = await self._run_child_agent(...)` → 解包 `run_result.output`，透传 `run_result.report` 到 metadata |
| `VoidxGraph._subagent_runner()` | `src/voidx/agent/adapters/langgraph/execution.py` | `result = await _run_subagent(...)` → 保留 `run_result`，`SubagentFinished` 的 `summary` 用 `run_result.output` |
| `run_subagent()` 内部所有 `return text` | `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | 每条终止路径改为 `return SubagentRunResult(...)`，见 §7 |

### 3. `AgentTool` 透传结构化 metadata

`AgentTool.execute()` 收到 `SubagentRunResult` 后返回：

```python
ToolResult(
    output=run_result.output,
    summary=f"{agent_def_name} completed",
    metadata={
        "agent": agent_def_name,
        "result_schema": normalized.result_contract.schema_name,
        "finish_reason": run_result.finish_reason,
        "status": run_result.status,
        "complete": run_result.complete,
        "contract_satisfied": run_result.contract_satisfied,
        "agent_result": run_result.report.model_dump(mode="json") if run_result.report else None,
    },
)
```

注意：`status == "incomplete"` 不应设置 `metadata.error=True`。工具进程成功返回，但任务结果质量不完整；父模型和 workflow 应通过 `metadata.status` / `metadata.agent_result` 判断。

### 4. workflow auto-advance 优先读结构化结果

> **当前状态**：`_check_review_result()`（`src/voidx/agent/application/automation/workflow/auto_advance.py`）通过 `metadata.get("agent") != "review"` 判断是否为 review 子 agent，再用 `_REVIEW_VERDICT_RE` 正则扫描 `output`。注意 `metadata["agent"]` 存的是 `agent_def_name`（如 `"voidx"`），不是 mode；当前能工作是因为 review 子 agent 的 `agent_def.name` 恰好为 `"review"`，但这不是稳定契约。

`auto_advance_events()` 的 review 判断顺序改为：

1. 如果 `metadata.result_schema == "review_result"`（由 `AgentTool.execute()` 从 `normalized.result_contract.schema_name` 设置，已存在于 `agent.py:236`），读取 `metadata.agent_result.verdict`。
2. `verdict in {"FAIL", "NEEDS_CHANGE"}` 时触发 `review_has_issues`。
3. 若缺少结构化 `agent_result`，再 fallback 到现有文本正则，同时保留 `metadata.get("agent") == "review"` 作为旧路径的 agent 标识。

判断条件从 `metadata["agent"]` 迁移到 `metadata["result_schema"]`，因为 `result_schema` 由 `AgentTool` 从 `result_contract` 确定性设置，不依赖 agent 命名约定。旧路径仍保留 `metadata["agent"]` 检查以兼容历史 session replay。

这样旧子 agent 和历史 session 仍可运行，新路径不再依赖输出格式。

### 5. 引入 terminal report

`should_continue=False` 只表示"不要再进入 call_llm/tool loop"，不再隐含"用户已经收到最终答复"。

#### 5.1 `AgentState` 新增字段

在 `AgentState`（`src/voidx/agent/state.py:14`）新增 `terminal_report` 字段：

```python
class AgentState(TypedDict):
    # ... existing fields ...
    convergence_forced: NotRequired[bool]
    terminal_report: NotRequired[dict[str, Any] | None]  # 子 agent 终止报告，供 _finalize 渲染
```

#### 5.2 写入点

`terminal_report` 在 `update_state_from_executed_tools()`（`src/voidx/agent/graph/tool_executor/workflow.py:82-96`）中写入。当 `auto_events` 触发 `review_has_issues` 且 `_advance_auto_events_for_route()` 返回 `should_stop=True` 时，在设置 `should_continue=False` 的同一分支同时写入 `terminal_report`：

```python
auto_events = _auto_advance_from_executed(executed, merged_workflow_runs)
if auto_events:
    merged_workflow_runs, stop_after_auto = _advance_auto_events_for_route(...)
    workflow_runs_changed = True
    if stop_after_auto:
        update["should_continue"] = False
        # 新增：从触发 auto-advance 的 tool result 中提取 terminal_report
        terminal = _extract_terminal_report(executed, auto_events)
        if terminal is not None:
            update["terminal_report"] = terminal
```

`_extract_terminal_report()` 遍历 `executed` 列表，找到 `tool_name == "agent"` 且 `metadata.result_schema == "review_result"` 且 `metadata.agent_result` 非空的 item，读取其 `metadata.agent_result` 和 `result.output`，组装为：

> 注意：`WorkflowStateEvent`（`workflow/types.py:43-50`）不携带 `tool_call_id`，无法直接关联到 `executed` item。因此 `_extract_terminal_report()` 需自行遍历 `executed` 按上述条件匹配。如果多个 agent tool 同时触发（并行子 agent），取第一个匹配项。

```python
terminal_report = {
    "kind": "review_has_issues",
    "source_tool_call_id": call_id,
    "title": "Review returned issues",
    "report": metadata["agent_result"],
    "fallback_text": result.output,
}
```

#### 5.3 `_finalize()` 消费

`_finalize()`（`src/voidx/agent/graph/core/llm.py:760-769`）新增优先级：

1. 如果 state 有 `terminal_report` 且非空，生成一条确定性的 final `AIMessage`，内容由 `render_terminal_report()` 渲染（见 §5.4）。
2. 否则沿用现有 `convergence_forced` fallback。
3. 否则不补消息。

这条 final message 不调用 LLM，不会触发工具，也不会改变 workflow 状态。它只是把已有子 agent 报告转成用户可见结果。

#### 5.4 `render_terminal_report()` 渲染模板

当 `agent_result.user_visible_report` 非空时直接使用；否则按以下模板确定性渲染：

```text
## Review: {title}

**Verdict:** {verdict}
**Status:** {status}

### Summary
{summary}

### Findings
{for each finding:}
- [{severity}] {title}
  {evidence}
  → {recommendation}

### Risks
{for each risk:}
- {risk}

### Next Actions
{for each action:}
- {action}
```

空字段省略对应小节。`fallback_text` 在 `report` 为 None 时使用。

### 6. UI 展示职责保持分离

- agent tool 节点继续展示 bounded preview，避免长报告刷屏。
- final assistant message 展示完整或规范化后的 terminal report。
- `SubagentFinished` header 继续显示执行状态和 finish reason。
- 后续如果支持"展开完整子 agent 报告"，应从 persisted tool result 或 subagent transcript 读取，不阻塞本设计。

### 7. 终止路径映射

`run_subagent()` 当前有 7 条终止路径（`src/voidx/agent/graph/subagent.py`）：6 条 `return text` 路径（`final_answer`、`contract_unsatisfied`、3 条 `guard_terminated`、`safety_limit`）和 1 条 `raise` 路径（`error`），每条都通过 `mark_finished(reason)` 设置 `finish_reason`。本设计新增第 8 条路径（`agent_report`），并将每条路径改为返回 `SubagentRunResult`。`status` / `complete` / `contract_satisfied` / `report` 按下表映射：

| 终止路径 | 代码行 | finish_reason | status | complete | contract_satisfied | report 来源 |
| --- | --- | --- | --- | --- | --- | --- |
| 子 agent 调用 `agent_report` | 新增 | `agent_report` | 从 `AgentReportInput.status` 取 | `status == "complete"` | `True` | `AgentReportInput` → `AgentStructuredReport` |
| 最终文本满足 contract | `subagent.py:288-289` | `final_answer` | `complete` | `True` | `True` | 合成 legacy report |
| 最终文本不满足 contract，retry 耗尽 | `subagent.py:283-284` | `contract_unsatisfied` | `incomplete` | `False` | `False` | 合成 legacy report |
| repetitive tools guard 终止 | `subagent.py:312-313` | `guard_terminated` | `incomplete` | `False` | `None` | 合成 legacy report |
| no progress guard 终止 | `subagent.py:425-426` | `guard_terminated` | `incomplete` | `False` | `None` | 合成 legacy report |
| wall clock guard 终止 | `subagent.py:435-436` | `guard_terminated` | `incomplete` | `False` | `None` | 合成 legacy report |
| safety step limit 达到 | `subagent.py:440-441` | `safety_limit` | `incomplete` | `False` | `None` | 合成 legacy report |
| 异常 | `subagent.py:447-448` | `error` | `failed` | `False` | `None` | `None` |

#### `agent_report` 与 safety limit 的交互

- 如果子 agent 在 safety limit 之前主动调用 `agent_report`，`finish_reason = "agent_report"`，`status` 和 `verdict` 由 `AgentReportInput` 决定。这是正常终止，不是 incomplete。
- 如果子 agent 命中 safety limit 之前**没有**调用 `agent_report`，走 `safety_limit` 路径，`status = "incomplete"`，`report` 由文本合成（legacy fallback）。
- 如果子 agent 调用了 `agent_report` 但 `AgentReportInput.status = "incomplete"`（子 agent 自报不完整），`finish_reason = "agent_report"`，`status = "incomplete"`，`complete = False`。这种情况下 `verdict` 仍可携带（如 `FAIL`），auto-advance 仍应触发 `review_has_issues`。

#### legacy report 合成规则

当子 agent 未调用 `agent_report` 而是返回文本时，`run_subagent()` 合成最小 report：

```python
AgentStructuredReport(
    schema_name=result_contract.schema_name,
    status=<按上表>,
    verdict=_legacy_verdict_from_text(output),  # 现有正则逻辑复用
    summary=_first_non_empty_line(output),
    user_visible_report=output,
)
```

`_legacy_verdict_from_text()` 复用 `auto_advance.py` 的 `_REVIEW_VERDICT_RE` 逻辑，从文本中提取 `verdict`，匹配不到则返回 `UNKNOWN`。

## Data Flow

> 以下描述的是**目标状态**（本设计实现后）。当前 `run_subagent()` 返回 `str`，`AgentTool.execute()` 直接消费字符串，auto-advance 依赖正则，`_finalize()` 只在 `convergence_forced` 时补消息。

```text
review child agent
  -> calls agent_report(review_result, verdict=NEEDS_CHANGE, ...)
run_subagent()
  -> [当前: return str] [目标: return SubagentRunResult(output, report, status=complete)]
AgentTool.execute()
  -> [当前: output=str] [目标: ToolResult(output=run_result.output, metadata.agent_result=report)]
GraphToolExecutor / update_state_from_executed_tools()
  -> ToolMessage(full output for compatibility)
  -> auto_advance reads metadata.result_schema == "review_result" then metadata.agent_result.verdict
  -> workflow review satisfied, feedback active
  -> should_continue=False
  -> terminal_report extracted from executed tool metadata
topology
  -> route_after_execute_tools returns "end" (should_continue=False)
  -> finalize
_finalize()
  -> [当前: only convergence_forced] [目标: terminal_report → AIMessage(render_terminal_report(report))]
```

## Compatibility Strategy

### Phase 1: Add structured path, keep fallback

- Add `agent_report` tool to child agent registry.
- `run_subagent()` accepts both:
  - structured finish via `agent_report`;
  - old plain final answer.
- If old final answer satisfies result contract, synthesize a minimal report:

```python
AgentStructuredReport(
    schema_name=result_contract.schema_name,
    status="complete",
    verdict=_legacy_verdict_from_text(output),
    summary=_first_non_empty_line(output),
    user_visible_report=output,
)
```

- `auto_advance` reads structured metadata first, regex second.

### Phase 2: Require report for workflow-sensitive modes

After review mode is stable:

- `review` must finish with `agent_report`.
- `feedback` and `implement` can still allow legacy text briefly.
- Contract retry guidance should explicitly say "call `agent_report` with schema_name=...".
  - 当前 `_result_contract_retry_message()`（`subagent.py` 中通过 `_RESULT_CONTRACT_RETRY_LIMIT = 2` 控制）在子 agent 返回不满足 contract 的文本时重新 prompt。Phase 2 后，retry message 应改为："Your previous answer did not use the required termination tool. Call `agent_report` with `schema_name=<contract.schema_name>` and include your findings."

### Phase 3: Remove regex dependency

Once all relevant modes use structured reports:

- Keep regex only for historical transcript replay or remove it behind a compatibility helper.
- Tests for `review_has_issues` should assert metadata-driven behavior.

## Error Handling

| Case | Behavior |
| --- | --- |
| Child calls `agent_report` with invalid schema | Return tool error inside child loop; prompt child once to correct |
| Child never calls `agent_report` but returns valid text | V1 synthesizes legacy report |
| Child reaches safety limit | Return `status="incomplete"`, no `verdict` unless known |
| Child report has `verdict=FAIL` but `status=incomplete` | Treat as issues found; terminal report should mention incomplete status |
| `user_visible_report` empty | Deterministically render from `summary`, `findings`, `risks`, `next_actions` |
| terminal report missing structured data | Use `fallback_text` |

## Testing

| Test | Expected |
| --- | --- |
| `test_review_agent_report_returns_structured_result` | `run_subagent()` returns `SubagentRunResult.report` from `agent_report` |
| `test_agent_tool_metadata_includes_agent_result` | `ToolResult.metadata.agent_result.verdict == "NEEDS_CHANGE"` |
| `test_auto_review_has_issues_uses_structured_metadata` | `review_has_issues` triggers without parsing `output` |
| `test_auto_review_has_issues_falls_back_to_legacy_text` | Existing text-only behavior still works |
| `test_review_terminal_report_finalizes_with_ai_message` | `should_continue=False` and final messages include user-visible report |
| `test_terminal_report_does_not_call_followup_llm` | No extra `call_llm` after review terminal report |
| `test_agent_report_tool_is_child_only` | Parent tool registry does not expose `agent_report` |
| `test_agent_report_terminates_child_loop` | Child cannot execute additional tools after report |

## Acceptance Criteria

- Review subagent findings are available as structured metadata.
- `FAIL` / `NEEDS_CHANGE` review results no longer require regex parsing in the primary path.
- A review issue terminal turn always produces a user-visible final assistant message.
- The graph does not perform a follow-up LLM call solely to summarize review output.
- Existing text-only child agent results remain compatible during migration.
- UI preview behavior for agent tool output remains bounded.

## Open Questions

- ~~Should `agent_report` be implemented as a real `BaseTool` in a child-only registry, or as a synthetic tool call intercepted by `run_subagent()` before normal dispatch?~~ **已决策**：synthetic tool call 拦截，见 §1.1。
- ~~Should `terminal_report` be part of `AgentState` formally, or carried as a generic state field until protocol v2?~~ **已决策**：正式加入 `AgentState`，见 §5.1。
- Should `AgentStructuredReport` live under `voidx.agent.graph.subagent` or a shared module such as `voidx.agent.subagent_result`?
- Should findings support file/line anchors in V1, or wait until review result inline comments are redesigned?
- How strict should schema validation be for `review_result` in Phase 1: generic report plus contract hints, or hard required `verdict`?
