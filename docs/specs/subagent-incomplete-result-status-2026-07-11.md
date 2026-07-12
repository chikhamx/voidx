# 子 Agent 不完整结果状态语义修正

> **Status: Approved**

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

### 2. `run_subagent` 返回结构化结果

当前 `run_subagent()` 返回 `str`，并通过 `run_metadata["finish_reason"]` 旁路传递原因。改为返回轻量结构：

```python
@dataclass(frozen=True)
class SubagentRunResult:
    output: str
    finish_reason: str
    status: Literal["complete", "incomplete", "failed"]
    complete: bool
    contract_satisfied: bool | None = None
```

**为什么不能用 `run_metadata` 旁路方案**：

`AgentTool.execute()` 通过 `self._run_child_agent(...)` 拿到的只是返回值 `str`，拿不到 `run_metadata`。`run_metadata` 是 `_subagent_runner` 内部的局部变量（`voidx_graph.py:599`），从未暴露给 AgentTool。要让 AgentTool 拿到 status，要么改返回类型，要么在 `_subagent_runner` 里把 status 塞进 AgentTool 可访问的位置——后者本质上又回到了改返回值结构。因此 `run_metadata` 旁路方案无法实现"AgentTool metadata 透传 status"这个目标，本 spec 明确选择改返回类型。

**改动面可控性分析**：

生产代码调用链只有一条，两个消费方实际是同一个函数：

```
run_subagent() -> SubagentRunResult
   ↑
   ├─ _subagent_runner()          (voidx_graph.py:621)  直接调用
   └─ AgentTool._run_child_agent  (agent.py:218)       通过 runner 注入间接调用
```

`_subagent_runner` 经 `build_tool_registry` → `register_agent_tool` → `AgentTool(runner=subagent_runner)` 注入到 AgentTool，所以两个消费方是同一个函数。

生产代码改动点（共 4 处，全在同一调用链上）：

| 位置 | 改动 |
| --- | --- | 
| `subagent.py:56` | 返回签名 `-> str` 改为 `-> SubagentRunResult`，所有 `return text` 改为 `return SubagentRunResult(output=text, ...)` |
| `subagent.py:162` `mark_finished` | 已有 `run_metadata["finish_reason"]`，只需补 `status`/`complete`/`contract_satisfied` |
| `voidx_graph.py:621-633` | `result = await _run_subagent(...)` → 从 `result.output` 取文本，从 `result.status` 设 `ok` |
| `agent.py:218-237` | `output = await self._run_child_agent(...)` → 从 `output.output` 取文本，metadata 补 status 字段 |

测试代码改动（约 15 处机械替换）：6 个测试文件（`test_subagent_step_budget*.py`、`test_subagent_llm_retry.py`、`test_prepare_workflow.py`）中 `output = await run_subagent(...)` 后跟 `assert "xxx" in output`，需改为 `assert "xxx" in output.output`。不涉及逻辑改动。

### 3. `_subagent_runner` 按状态发送事件

`SubagentFinished` 目前只有 `ok: bool`（`schema.py:237-244`）。增加可选 `status` 字段，并让 `ok` 只代表 `status == "complete"`：

```python
SubagentFinished(
    ok=status == "complete",
    finish_reason=finish_reason,
    summary=result.output,
    status=status,
)
```

保留 `ok` 并新增可选 `status`，向后兼容。前端验证：`frontend/src/render.ts:195` 的 `renderSubagentCard` 只消费 `node.payload.name`/`node.agent_name`/`elapsed`/`steps`，不消费 `ok` 或 `finish_reason`；`frontend/src/protocol.d.ts` 无 `SubagentFinished` 的 TypeScript 类型定义。因此新增 `status` 字段对前端无破坏性影响，前端改动为零或极小。

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

对 `status == "incomplete"` 的情况，**不设置** `metadata.error=True`，否则 runtime guard 会把它当作工具失败（`agent.py` 中 error metadata 走失败路径）。改用显式字段 `incomplete=True`。

`ToolMessage.status` 的决策：incomplete 不映射为 `"error"`。`tool_executor/executor.py` 中父模型收到的 `ToolMessage.status` 只根据工具执行是否 ok 设置为 `"success"` 或 `"error"`，对 agent 工具来说 incomplete 仍应为 `"success"`——进程正常返回，结果部分可用。父模型通过 metadata 中的 `status`/`incomplete` 字段判断结果质量，而非依赖 `ToolMessage.status`。

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

这需要 `_execute_approved_batch()`（`tool_executor/helpers.py:660-673`）能从 agent 工具结果 metadata 中统计 `status`。当前 `StatusFinished` 的 label 是静态字符串，改为根据已执行结果动态拼接计数。实现路径：在 `helpers.py:667` 处 `executed` 列表已包含所有 `ToolResult`，遍历其 `metadata.get("status")` 即可统计 complete/incomplete/failed 计数。

## 实施建议

### Phase 1：只修状态语义，不改调度策略

- `subagent.py`：定义 `SubagentRunResult` dataclass，`run_subagent()` 返回签名改为 `-> SubagentRunResult`，`mark_finished()` 补充 `status`/`complete`/`contract_satisfied` 到 `run_metadata`，所有 `return text` 改为 `return SubagentRunResult(output=text, ...)`
- `voidx_graph.py:621-633`：`_subagent_runner` 从 `result.output` 取文本，从 `result.status` 设 `ok = (status == "complete")`，`SubagentFinished` 新增 `status` 字段
- `schema.py:237-244`：`SubagentFinished` 增加可选 `status: str = ""` 字段
- `agent.py:218-237`：`AgentTool.execute` 从 `run_result.output` 取文本，metadata 补充 `finish_reason`/`status`/`complete`/`contract_satisfied`/`incomplete`
- `consumers.py:386`：UI 渲染从 `label = "completed" if e.ok else "failed"` 改为三态
- `helpers.py:660-673`：并行汇总从 `executed` 列表的 metadata 统计 complete/incomplete/failed 计数
- 测试：6 个测试文件约 15 处 `assert "xxx" in output` 改为 `assert "xxx" in output.output`

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

- ~~如果把 incomplete 映射为 `ToolMessage.status="error"`，可能触发现有工具失败 guard，导致父 agent 过早进入失败恢复路径。~~ **已决策**：incomplete 不映射为 `"error"`，保持 `"success"`，父模型通过 metadata 判断。
- ~~如果只改 UI 文案，不改 metadata，父模型仍然无法可靠判断哪些结果可采纳。~~ **已决策**：Phase 1 同时改 UI 文案和 metadata。
- ~~如果修改 `SubagentFinished` schema，需要同步更新 frontend protocol schema 和类型定义。~~ **已验证**：前端不直接消费 `SubagentFinished` 事件字段，新增 `status` 对前端无破坏性影响。
- ~~保留 `ok` 并新增 `status` 是较低风险兼容方案。~~ **已采纳**。

## 开放问题

- `guard_terminated` 应统一算 incomplete，还是按具体 guard 类型细分？Phase 1 建议统一算 incomplete，但在 metadata 中保留 `guard_kind` 诊断字段（`subagent.py` 中有 3 处 `mark_finished("guard_terminated")`：line 312 为 runtime guard，line 425/435 为其他 guard 场景），便于后续细分而不破坏当前三态语义。
- `contract_unsatisfied` 是否应该触发主 agent 自动重跑，还是只提供 next-step hint？
- `safety_limit` 是否需要暴露最后一步、工具计数、用时等诊断 metadata？
- 子 agent 完整 transcript 是否应该提供可点击入口，避免主 agent 依赖截断预览判断？

