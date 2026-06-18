# Checkpoint 工具 state_patch 不完整导致工作流状态不一致 — 问题分析报告

## Context

当 LLM 在 debug 工作流中调用 `checkpoint` 工具并获得用户批准后，系统出现"自动退出"现象：LLM 输出一段文字后不再调用工具，turn 提前结束。根因是 checkpoint 的 `state_patch` 缺少 `workflow_runs`，导致工作流状态不一致。

## 问题 1（核心 bug）：checkpoint 的 state_patch 缺少 workflow_runs

### 现象

checkpoint 批准后，下一轮 turn 中 debug 和 tdd 两个工作流节点同时处于 ACTIVE 状态，两套指令同时注入 prompt，LLM 行为混乱。

### 根因链路

1. `plan_checkpoint.py:107-113` 中，approved 决策只设置了三个字段：

```python
patch = ToolStatePatch(
    intent=IntentResolution(type=TaskIntent.CODING, desc=scope),
    goal=GoalSpec(type=GoalType.FEATURE, desc=scope),
    plan=PlanResolution(join="tdd", leave="verify"),
)
```

`workflow_runs` 字段未设置（默认空列表），`model_fields_set` 中不包含 `workflow_runs`。

2. `tool_executor.py:738-770` 处理 state_patch 时，只处理 `patch.model_fields_set` 中的字段。由于 `workflow_runs` 不在 `model_fields_set` 中，当前 debug 节点保持 ACTIVE 不变。

3. `plan=PlanResolution(join="tdd", leave="verify")` 被写入 `workflow_route`。

4. 下一轮 `_prepare_with_stream`（`core.py:653-718`）执行时：
   - `existing_workflow_runs` 从 `task_state.workflow_runs` 取出 → 包含 debug(ACTIVE)
   - `workflow_start = task_state.workflow_route.join` → `"tdd"`
   - `select_from_start("tdd")` 返回 tdd 节点
   - `active_names = active_workflow_names(existing_workflow_runs)` → 包含 `"debug"`
   - `active = _merged_names(active_names, [match.name for match in matches])` → 包含 `"debug"` 和 `"tdd"`
   - **两个工作流的指令同时注入 prompt**

5. `_merge_workflow_runs(existing_workflow_runs, workflow_context.runs)` 合并后 → debug(ACTIVE) + tdd(ACTIVE) 共存。

### 影响

- LLM 同时收到 debug 和 tdd 两套指令，行为不可预测
- debug 节点永远不会被自动标记为 SATISFIED，除非 LLM 显式调用 `workflow` 工具
- 工作流路由指向 tdd，但 debug 仍在活跃，语义矛盾

### 对比：workflow 工具的正确做法

`workflow.py` 中 advance 操作（`tool_executor.py:844-878`）会：
1. 将当前节点标记为 SATISFIED
2. 通过 `_satisfy_workflow_without_transition` 更新 `workflow_runs`
3. 设置 `should_continue = False`（路由终端节点时）

checkpoint 缺少这一步。

## 问题 2（原分析有误）：next_step_hint 在 approved/needs_doc 场景中不必要

### 重新审视

原分析认为 `next_step_hint` 太模糊导致 LLM 不调用工具。但实际流程是：

1. checkpoint 批准后，`state_patch` 设置 `workflow_route = {join: "tdd", leave: "verify"}`
2. 下一轮 `_prepare_with_stream` 根据 `workflow_route.join` 自动创建 tdd 节点
3. tdd 工作流的完整指令被注入 prompt，包括工具使用规则、TDD 流程等
4. LLM 拿到的 prompt 里已经有足够的工作流指令来引导行为

因此，**approved / needs_doc 场景下 `next_step_hint` 是多余的**——工作流指令本身已经足够引导 LLM。真正的问题不是 hint 不够明确，而是问题 1 导致的工作流状态不一致（debug 和 tdd 同时 ACTIVE），使得 LLM 收到混乱的指令。

### 修正结论

- **approved / needs_doc**：`next_step_hint` 可以去掉或留空。修复问题 1 后，工作流指令会正确引导 LLM。
- **modified**：用户修改了范围但没有明确方向，LLM 需要自己判断。当前为空合理，也可以考虑加 hint 提醒 LLM 根据修改后的 scope 重新规划。
- **rejected**：空 hint 合理。

### 关于 LLM 不调用工具就停下来的问题

这个问题在修复问题 1 后应该自然解决：
- 当前根因是 debug 和 tdd 同时 ACTIVE，两套指令冲突导致 LLM 困惑
- 修复后只有 tdd ACTIVE，LLM 收到清晰的 TDD 工作流指令，自然会开始调用工具
- 如果修复后仍有 LLM 不调用工具的情况，再单独处理

## 修复方向

### 修复 1：checkpoint 的 state_patch 包含 workflow_runs

在 `_decision_result` 中，当 decision 为 `approved` 或 `needs_doc` 时：

1. 接收当前 `workflow_runs`（从 `ctx.workflow_runs` 传入）
2. 将当前活跃节点标记为 SATISFIED
3. 创建目标节点（tdd 或 design）为 ACTIVE
4. 将更新后的 `workflow_runs` 写入 `ToolStatePatch`

具体实现：
- 修改 `_decision_result` 签名，增加 `workflow_runs` 参数
- 在 `execute` 方法中传入 `ctx.workflow_runs`
- 对 approved：satisfy 当前活跃节点 + 创建 tdd(ACTIVE)
- 对 needs_doc：satisfy 当前活跃节点 + 创建 design(ACTIVE)

### 修复 2：清理 next_step_hint

approved / needs_doc 场景下，工作流指令已足够引导 LLM，`next_step_hint` 多余。将其清空，避免与工作流指令冲突或产生歧义。

### 修复 3（可选）：考虑 should_continue

当 checkpoint 批准且工作流路由终端被满足时，`_explicit_advance_route_limited_runs` 会设置 `should_continue = False`。这在 workflow 工具中是合理的（advance 后 turn 结束，下一轮从新节点开始），但对 checkpoint 可能不合适——我们希望 LLM 在同一 turn 内继续工作。

需要确认：修复 1 后，`_collect_state_update_from_executed` 中的逻辑是否会误触发 `should_continue = False`。如果会，需要在 checkpoint 场景中避免。

## 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/voidx/tools/plan_checkpoint.py` | `_decision_result` 增加 workflow_runs 参数，approved/needs_doc 时构建完整 state_patch |
| `tests/test_tools/test_interactive_tools.py` | 更新现有测试，验证 state_patch 包含 workflow_runs |

## 风险

| 风险 | 缓解措施 |
|------|---------|
| 修改 `_decision_result` 签名影响现有调用点 | 只增加可选参数，默认空列表，向后兼容 |
| workflow_runs 中目标节点的 persona 信息需要从 DAG 获取 | 使用 `WorkflowService.get()` 查询，或直接硬编码已知映射 |
| should_continue 误设为 False | 验证修复后 checkpoint 批准不会触发路由终端逻辑 |
