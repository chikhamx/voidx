# Workflow Terminal Turn Finalization 代码审查报告

> **审查日期**: 2026-06-14
> **审查范围**: `tool_executor.py`, `topology.py`, `state.py`, `core.py`, `test_core_flow.py`
> **设计文档**: `docs/archive/workflow-terminal-turn-finalization-2026-06-14.md`
> **Verdict**: PASS ✅

## 变更概述

当 `advance_workflow(done)` 成功关闭所有活跃 workflow 节点后，跳过后续不必要的 LLM 调用，直接路由到 `finalize`。

## 变更文件

| 文件 | 变更 |
|---|---|
| `src/voidx/agent/state.py` | 新增 `should_continue: bool` 路由标志 |
| `src/voidx/agent/graph/topology.py` | 新增 `route_after_execute_tools` 条件路由，`execute_tools` 边改为条件路由 |
| `src/voidx/agent/graph/tool_executor.py` | 新增 `_terminal_workflow_completed` 检测函数，批处理完成后设置 `should_continue=False` |
| `src/voidx/agent/graph/core.py` | `_call_llm` 在无模型/LLM 错误时设置 `should_continue=False` |
| `tests/test_agent/test_core_flow.py` | 4 个新测试覆盖设计文档的测试矩阵 |

## 需求对照

| 设计要求 | 状态 |
|---|---|
| 终端 `advance_workflow(done)` 返回 `should_continue=False` | ✅ 测试覆盖 |
| 非终端 `advance_workflow(implemented)` 保持继续 | ✅ 测试覆盖 |
| 多个 `advance_workflow(done)` 关闭所有活跃节点 | ✅ 测试覆盖 |
| 拓扑路由 `should_continue=False` → `finalize` | ✅ 测试覆盖 |
| 保留工具批处理语义 | ✅ 批处理完整执行后才检查 |
| 失败的 `advance_workflow` 不触发停止 | ✅ `result_ok` 检查 + `denied/blocked` 前置守卫 |

## 风险点

1. **`_terminal_workflow_completed` 使用 `break`** — 找到第一个终端 `advance_workflow` 即停。当前语义只需"至少一个"，正确。若未来需校验全部调用均为终端，需改为全遍历。
2. **`should_continue` 无默认值** — `route_after_execute_tools` 用 `state.get()` 缺失时回退 `call_llm`，防御性设计合理。
3. **`runtime_workflow_runs` 状态链路** — `apply_state_update` 通过 nonlocal 在每个 segment 后更新，`_terminal_workflow_completed` 接收最终合并状态，链路正确。

## 结论

实现与设计文档完全对齐，测试矩阵全覆盖，边界条件处理正确，状态链路一致。代码简洁，无过度设计。
