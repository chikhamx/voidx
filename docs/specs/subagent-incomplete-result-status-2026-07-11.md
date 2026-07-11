# 子 Agent 不完整结果状态语义修正

> **Status: Draft**

## 来源

来自一次并行子 agent 深度扫描的实际运行现象：

- UI 中多个子 agent 显示 `completed (safety limit, ...)` 或 `completed (contract unsatisfied, ...)`
- 主 agent 随后判断"部分子 agent 返回了不完整的结果"，需要重新检查哪些完成、哪些需要重跑
- 截图里同时出现两类提示：
  - "三个子 agent 已完成但输出被截断"
  - "另外三个（agent、tools、infra）的输出不完整"

## 问题概述

当前实现把"子 agent 进程正常返回"和"子 agent 任务完整成功"混为一谈。

子 agent 命中 `safety_limit`、`contract_unsatisfied`、`guard_terminated` 时没有抛异常，而是正常返回文本。父层 `_subagent_runner` 只要 `_run_subagent(...)` 没有抛异常，就设置 `ok=True`，UI 也据此渲染为 `completed`。这导致运行状态、UI 文案、父模型收到的工具结果三者都无法稳定表达"结果不完整"。

## 现象拆解

### 1. 已完成但输出被截断

**用户可见现象**：

子 agent 行显示类似：

```text
completed (safety limit, 1220.4s)
completed (contract unsatisfied, 1172.0s)
```

但主 agent 又提示这些子 agent "已完成但输出被截断"。

**代码链路**：

- `src/voidx/agent/graph/subagent.py`
  - `_SAFETY_STEP_LIMIT = 50`
  - `contract_unsatisfied`、`guard_terminated`、`safety_limit` 都会 `return text`
  - 这些 return 不区分完整成功与部分结果
- `src/voidx/agent/graph/core/voidx_graph.py`
  - `_subagent_runner()` 中 `_run_subagent(...)` 正常返回后直接设置 `ok = True`
  - `SubagentFinished(ok=True, finish_reason=...)` 被发送给 UI
- `src/voidx/ui/output/events/consumers.py`
  - `SubagentFinished` 渲染逻辑使用 `label = "completed" if e.ok else "failed"`
  - 因此 `ok=True + finish_reason=safety_limit` 被渲染成 `completed (safety limit)`

**根因**：

`finish_reason` 只是附加说明，不参与成功/失败/不完整状态判定。

### 2. 另外三个输出不完整

**用户可见现象**：

主 agent 能意识到有些子 agent 输出不完整，但需要再检查哪些完成、哪些要重取。

**代码链路**：

- `src/voidx/tools/agent.py`
  - `AgentTool.execute()` 调用 `_run_child_agent(...)` 后统一返回：

```python
ToolResult(
    output=output,
    summary=f"{agent_def_name} completed",
    metadata={
        "agent": agent_def_name,
        "intent": ...,
        "goal": ...,
        "workflow_route": ...,
        "result_schema": ...,
    },
)
```

  - metadata 中没有 `finish_reason`、`complete`、`contract_satisfied`、`incomplete` 等结构化字段
- `src/voidx/agent/graph/tool_executor/executor.py`
  - 父模型收到的 `ToolMessage.status` 只根据工具执行是否 ok 设置为 `"success"` 或 `"error"`
  - 对 `agent` 工具来说，子 agent 任务不完整仍可能作为成功工具结果写回父上下文
- `src/voidx/agent/graph/tool_executor/helpers.py`
  - 并行汇总状态只显示 `Finished N child agents`
  - 不区分多少 complete、多少 incomplete、多少 failed

**根因**：

子 agent 的结果质量没有结构化地传递给 `AgentTool` 和主执行器，父模型只能从文本里猜测不完整性。

## 影响

- **UI 误导**：`completed (safety limit)` 语义自相矛盾，用户很难判断是否需要重跑。
- **父模型判断不稳定**：父模型收到的是 success tool message，但内容可能是部分结果。
- **并行任务汇总不可诊断**：`Finished 6 child agents` 无法说明其中几个可采纳、几个需要补跑。
- **后续推理成本上升**：主 agent 需要额外扫描子 agent 状态和文本，才能判断哪些结果可用。

## 目标

- 建立子 agent 三态结果语义：
  - `complete`：合同满足，任务完整完成
  - `incomplete`：进程正常结束，但结果不完整或不可完全采纳
  - `failed`：异常、timeout 或工具执行失败
- 让 UI、`AgentTool` metadata、父模型 `ToolMessage` 对这些状态保持一致。
- 让并行汇总能展示 complete / incomplete / failed 计数。
- 避免仅通过字符串解析 `finish_reason` 来判断结果质量。

## 非目标

- 不调整子 agent 的扫描策略、token budget 或 wall-clock 限制。
- 不改变 agent 工具的基本输入 schema。
- 不在本 spec 中解决大输出持久化、分页查看或重新获取完整 transcript 的问题。
- 不改变普通工具调用的 success/error 语义，除非该工具是 `agent`。

## 建议设计

### 1. 引入子 agent 结果状态

建议定义结构化状态，初步映射如下：

| finish_reason | status | complete | contract_satisfied | 说明 |
| --- | --- | --- | --- | --- |
| `final_answer` | `complete` | `true` | `true` | 正常完整结果 |
| `contract_unsatisfied` | `incomplete` | `false` | `false` | 输出不满足 result contract |
| `safety_limit` | `incomplete` | `false` | unknown | 步数上限触发，最后文本不可视为完整 |
| `guard_terminated` | `incomplete` | `false` | unknown | runtime guard 终止，可能只有诊断文本 |
| `error` | `failed` | `false` | unknown | 异常 |
| `timeout` | `failed` | `false` | unknown | timeout |

### 2. `_run_subagent` 返回结构化结果

当前 `_run_subagent()` 返回 `str`，并通过 `run_metadata["finish_reason"]` 旁路传递原因。建议改为返回轻量结构，例如：

```python
@dataclass(frozen=True)
class SubagentRunResult:
    output: str
    finish_reason: str
    status: Literal["complete", "incomplete", "failed"]
    complete: bool
    contract_satisfied: bool | None = None
```

如果要降低改动面，也可以暂时保留 `str` 返回，但在 `_subagent_runner()` 中集中用 `finish_reason` 映射状态，并把状态继续传给 `AgentTool`。

### 3. `_subagent_runner` 按状态发送事件

`SubagentFinished` 目前只有 `ok: bool`。建议增加 `status` 字段，或至少让 `ok` 只代表 `status == "complete"`：

```python
SubagentFinished(
    ok=status == "complete",
    finish_reason=finish_reason,
    summary=result.output,
    status=status,
)
```

若考虑前端协议兼容，可先保留 `ok`，新增可选 `status`。

### 4. `AgentTool` metadata 透传结果质量

`AgentTool.execute()` 返回的 metadata 建议补充：

```python
metadata={
    "agent": agent_def_name,
    "result_schema": normalized.result_contract.schema_name,
    "finish_reason": run_result.finish_reason,
    "status": run_result.status,
    "complete": run_result.complete,
    "contract_satisfied": run_result.contract_satisfied,
}
```

对 `status == "incomplete"` 的情况，建议不要设置通用 `metadata.error=True`，否则 runtime guard 可能把它当作工具失败；更适合显式字段 `incomplete=True`。是否让 `ToolMessage.status="error"` 需要单独权衡。

### 5. UI 文案改为三态

`src/voidx/ui/output/events/consumers.py` 中的渲染建议：

- `complete` → `completed`
- `incomplete` → `incomplete`
- `failed` → `failed`

示例：

```text
Reviewer(...) incomplete (safety limit, 1220.4s)
Reviewer(...) incomplete (contract unsatisfied, 1172.0s)
Reviewer(...) completed (final answer, 314.2s)
```

### 6. 并行汇总显示计数

当前并行汇总只显示：

```text
Finished 6 child agents
```

建议改为：

```text
Finished 6 child agents · 3 complete · 3 incomplete
```

这需要 `_execute_approved_batch()` 能从 agent 工具结果 metadata 中统计 `status`。

## 实施建议

### Phase 1：只修状态语义，不改调度策略

- 修改子 agent finish reason 到 status 的集中映射函数
- `_subagent_runner()` 生成 complete/incomplete/failed 语义
- `SubagentFinished` 增加或派生三态信息
- UI 渲染改为三态文案
- `AgentTool` metadata 透传 `finish_reason/status/complete`

### Phase 2：让父模型更容易恢复

- 在 incomplete agent 工具结果中增加 `next_step_hint`
- 对 `contract_unsatisfied` 提示父模型可用更窄 target 重跑
- 对 `safety_limit` 提示父模型优先读取子 agent transcript 或缩小扫描范围

### Phase 3：改进并行汇总

- 并行 agent 状态汇总统计 complete / incomplete / failed
- 对 incomplete 子 agent 在 UI 中保留醒目但非 fatal 的状态

## 测试建议

### 单元测试

- `src/tests/test_agent/graph/test_subagent_runner.py`
  - `final_answer` → `SubagentFinished(status=complete, ok=True)`
  - `contract_unsatisfied` → `status=incomplete`
  - `safety_limit` → `status=incomplete`
- `src/tests/test_agent/graph/test_execute_tools_guard.py`
  - agent tool incomplete metadata 能写入父 ToolMessage 相关上下文
- `tui/tests/test_status_activity.py` 或相关 output tests
  - `incomplete (safety limit)` 不再渲染为 `completed`
- `frontend/test/render.test.ts`
  - 如果前端也消费 `SubagentFinished.status`，补三态渲染测试

### 集成测试

- 构造 fake 子 agent 返回 `finish_reason=contract_unsatisfied`
- 验证：
  - UI 节点显示 incomplete
  - `AgentTool` metadata 包含 incomplete 状态
  - 父模型收到的工具结果文本中能看到结构化状态提示

## 风险与权衡

- 如果把 incomplete 映射为 `ToolMessage.status="error"`，可能触发现有工具失败 guard，导致父 agent 过早进入失败恢复路径。
- 如果只改 UI 文案，不改 metadata，父模型仍然无法可靠判断哪些结果可采纳。
- 如果修改 `SubagentFinished` schema，需要同步更新 frontend protocol schema 和类型定义。
- 保留 `ok` 并新增 `status` 是较低风险兼容方案。

## 开放问题

- `guard_terminated` 应统一算 incomplete，还是按具体 guard 类型细分？
- `contract_unsatisfied` 是否应该触发主 agent 自动重跑，还是只提供 next-step hint？
- `safety_limit` 是否需要暴露最后一步、工具计数、用时等诊断 metadata？
- 子 agent 完整 transcript 是否应该提供可点击入口，避免主 agent 依赖截断预览判断？

