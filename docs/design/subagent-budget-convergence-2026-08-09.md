# Subagent Budget Convergence & Failure Recovery

Date: 2026-08-09

> **Status: Design draft, awaiting review**
>
> 改进子 agent 运行机制：预算配置化、软性收敛、上下文极限收敛、
> 工具失败恢复、崩溃兜底。目标是在硬性 guard 掐断之前让子 agent
> 主动收尾，减少 token 浪费与"运行中中断"。

## 1. Problem

子 agent（`agent` 工具派生的 child run）在以下场景会浪费 token 或直接中断：

1. **时间/步骤硬掐断**：墙钟 30 分钟（`runtime_guards.py:272` `WallClockGuardState.for_subagent()`）
   或 100 步（`subagent.py:74` `_SAFETY_STEP_LIMIT`）到点直接 `terminate`，
   无任何提前预警，子 agent 可能正在产出中途被掐断，已消耗的 token 无法回收。
2. **LLM 调用崩溃**：`NON_RETRYABLE` / `CONTEXT_OVERFLOW` 错误直接 `raise`
   （`subagent.py:483-486`），整个子 agent 异常退出，已收集的进展全部丢失。
3. **工具失败死局**：同一工具调用失败 3 次被 `blocked_call_keys` 阻断
   （`runtime_guards.py:110-123`），子 agent 可能失去关键工具后陷入
   无进展循环，直到连续 5 轮无进展被终止（`runtime_guards.py:207`）。
4. **上下文无收敛**：子 agent 无 compaction，每步估算的
   `context_tokens`（`subagent.py:445-449`）只用于记账，不做收敛决策，
   等到 `CONTEXT_OVERFLOW` 时已经晚了。

## 2. Current Behavior (code map)

| 行为 | 位置 | 现状 |
| --- | --- | --- |
| 步骤上限 | `subagent.py:74` `_SAFETY_STEP_LIMIT = 100` | 硬编码，到点 `safety_limit` 结束 |
| 墙钟上限 | `runtime_guards.py:271-272` `for_subagent()` → `limit_seconds=1800.0` | 硬编码，到点 `guard_terminated` |
| 无进展终止 | `runtime_guards.py:207` `terminate_threshold = 5` | 连续 5 轮无进展终止 |
| 工具失败阻断 | `runtime_guards.py:110-123` | 相同调用失败 3 次入 `blocked_call_keys` |
| LLM 错误处理 | `subagent.py:481-505` | 可重试错误最多 10 次退避重试；`NON_RETRYABLE`/`CONTEXT_OVERFLOW` 直接 raise |
| 上下文估算 | `subagent.py:445-449` `estimate_context_tokens_with_tools` | 仅更新 `usage_stats`，无阈值决策 |
| 预算配置 | `config/models.py` `Config` | 无子 agent 预算字段，`SubagentConfig` Protocol（`subagent.py:93-104`）无预算成员 |
| 终止汇报 | `subagent.py:740-758` `_guard_termination_result` | guard 终止时提取已有消息文本返回 |

## 3. Goals

- 子 agent 预算（步骤/时间/上下文）可配置，默认值保持现有行为。
- 预算接近上限时先注入收敛引导（软性收敛），子 agent 主动收尾，而不是被硬掐断。
- `CONTEXT_OVERFLOW` 不再 raise，改为优雅收尾，保留已有进展。
- timeout 类工具失败自动重试一次，减少瞬时错误导致的死局。
- 非 fatal 的 LLM/运行时错误返回已有进展而非异常，父 agent 拿到结果而非中断。
- 全部改动有测试覆盖，默认路径行为不变。

## 4. Non-Goals

- 不引入子 agent compaction/摘要机制（子 agent 短命运行，收敛优先于压缩）。
- 不改 `CompactionService`（父 agent 压缩逻辑不动）。
- 不改 `_BLOCKED_CHILD_TOOLS` 工具面（`{"agent", "clarify", "checkpoint"}`）。
- 不重做 workflow DAG / 汇报协议（`docs/design/subagent-report-protocol.md` 另行处理）。
- 不改父 agent 的 guard 行为（`for_subagent()` 与父 agent 路径分离）。

## 5. Design

### 5.1 预算配置化

`src/voidx/config/models.py` `Config` 新增字段（默认值与现硬编码一致）：

```python
subagent_step_limit: int = Field(default=100, ge=1, le=1000)
subagent_wall_clock_seconds: float = Field(default=1800.0, ge=1.0, le=86400.0)
subagent_context_soft_ratio: float = Field(default=0.75, ge=0.1, le=0.95)
subagent_soft_warn_ratio: float = Field(default=0.80, ge=0.1, le=1.0)
```

`subagent.py` 的 `SubagentConfig` Protocol 增加同名成员（`int`/`float`），
`run_subagent()` 读取：

- `_SAFETY_STEP_LIMIT` 替换为 `config.subagent_step_limit`（循环条件
  `while step < config.subagent_step_limit`，`subagent.py:432`）。
- `WallClockGuardState.for_subagent()` 改为接收
  `limit_seconds=config.subagent_wall_clock_seconds`
  （`subagent.py:196`）。

### 5.2 软性收敛（软预算预警）

在 `run_subagent()` 主循环内、每步 LLM 调用前（复用 `pending_guard_guidance`
机制，`subagent.py:420-428`），按序检查以下阈值，首次越过时注入一条
GUIDANCE 引导子 agent 收尾：

| 预算 | 触发条件 | 注入文案要点 |
| --- | --- | --- |
| 步骤 | `step >= subagent_step_limit * subagent_soft_warn_ratio` | "步骤预算剩余约 X%，请收敛：完成当前目标并总结结果。" |
| 墙钟 | `elapsed >= subagent_wall_clock_seconds * subagent_soft_warn_ratio` | "时间预算剩余约 X%，请收敛并输出最终结果。" |
| 上下文 | `context_tokens >= usage_stats.context_limit * subagent_context_soft_ratio` | "上下文接近极限，请尽快收敛，避免上下文溢出。" |

实现方式：新增 `_soft_warn_injected: set[str]`（键如 `"step"`/`"wall"`/`"context"`），
每个键只注入一次，避免每轮重复刷屏。注入消息带 `GUIDANCE_MARKER`
（与现有 guard guidance 一致，`subagent.py:424-427`）。

上下文估算已存在（`subagent.py:445-449` 的 `context_tokens`），
在估算后追加检查。

### 5.3 上下文极限收敛

- `CONTEXT_OVERFLOW` 不再 `raise`：在 `subagent.py:483-486` 的分支中，
  改为提取已有消息文本（`_guard_termination_result` 风格），
  以 `finish_reason="context_limit"` 汇报返回（复用 `report_result`）。
- 语义：上下文溢出是模型侧拒绝，子 agent 已做的工作仍有效，
  返回最后消息文本与 guard 终止一致。
- 保持 `NON_RETRYABLE` 走崩溃兜底（见 5.5），其余可重试错误逻辑不变。

### 5.4 工具失败恢复

- **timeout 自动重试一次**：在 `subagent.py` 主循环执行工具后的失败处理
  （`subagent.py:703-717`）中，若失败结果 metadata 带
  `timeout=True`（`voidx.tooling.domain.result.tool_timeout_metadata` 设置
  `metadata={"error": True, "timeout": True, "exit_code": -1}`），
  且该工具调用尚未重试过，则自动重发该调用一次（不消耗 LLM 轮次）。
- 记录方式：`retried_tool_calls: set[str]`（key 为 tool_call id），
  第二次仍失败则走正常失败处理（计入 `tool_failures` guard）。
- **失败 guidance 带原因分类**：`subagent.py:711-717` 构造 guidance 时，
  在 `build_failure_key` 的 `error_kind` 基础上，追加一句原因提示：
  - policy/sandbox 拒绝（`metadata.blocked`）→ "该调用被策略拒绝，换一个方式。"
  - timeout → "工具超时，已重试仍失败，换更小粒度的操作。"
  - 其余 → 维持现有文案。

### 5.5 崩溃兜底

- `run_subagent()` 外层 `except Exception`（`subagent.py:765-770`）：
  若 `_classify_llm_error(e)` 为 `NON_RETRYABLE`，或错误来自工具执行
  （`ToolResult` 已转错误消息，不会走到这里），构造兜底结果：
  提取 `messages` 最后一条 assistant 文本（`extract_text`），
  以 `finish_reason="error_recovered"` 汇报返回，而不是 `raise`。
- 保留真正 fatal 的异常（如 `RuntimeError("model_factory is required")`、
  配置错误）继续 `raise`：仅在 LLM 调用层错误（`NON_RETRYABLE`/
  `CONTEXT_OVERFLOW`，已在 5.3 处理前者）或可识别错误时兜底。
- 实现：`except Exception as e:` 分支内先 `kind = _classify_llm_error(e)`，
  若 `kind in {LLMErrorKind.NON_RETRYABLE, LLMErrorKind.CONTEXT_OVERFLOW}`
  （后者理论已被 5.3 拦截，双保险），走 `report_result` + `mark_finished("error_recovered")` + `return text`。

### 5.6 UI/追踪一致性

- 所有新 finish_reason（`context_limit`、`error_recovered`）透传到
  `execution.py:1000-1008` 的 `SubagentFinished` 事件（已是
  `run_metadata["finish_reason"]` 通道），无需额外改动。
- `tracker` 在兜底路径同样 `finish(task_id, "completed")`，
  避免遗留运行中的 task。

## 6. File Changes

| 文件 | 变更 |
| --- | --- |
| `src/voidx/config/models.py` | `Config` 新增 4 个预算字段 |
| `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | Protocol 扩展；读取预算；软收敛注入；CONTEXT_OVERFLOW/崩溃兜底；timeout 重试；失败分类 guidance |
| `src/voidx/agent/adapters/langgraph/runtime/runtime_guards.py` | `for_subagent(limit_seconds=...)` 参数化（默认 1800 不变） |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget.py` | 配置化预算、软收敛注入测试 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_llm_retry.py` | CONTEXT_OVERFLOW 优雅收尾、崩溃兜底测试 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_runner.py` | timeout 工具重试、失败分类 guidance 测试 |

## 7. Verification

```bash
# 聚焦测试
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget.py
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget_final.py
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_llm_retry.py
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_runner.py

# 回归：runtime 全部子 agent 测试
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/ -k subagent

# 后端全套（验证默认路径行为不变）
./test.py --backend
```

## 8. Risks & Trade-offs

- 默认值保持现行为（100 步 / 1800s / 0.75 / 0.80），配置化不改变现有运行结果。
- 软性收敛注入 guidance 增加少量 token，但避免硬掐断的更大损失。
- `CONTEXT_OVERFLOW` 优雅收尾改变异常传播语义：父 agent 可能收到不完整结果，
  通过 `finish_reason="context_limit"` 显式标记，父层可识别。
- timeout 自动重试只针对瞬时错误（`timeout=True`），策略拒绝（`blocked`）
  不重试，避免重复无效请求。
- `error_recovered` 兜底可能掩盖真实 LLM 错误细节：文本中保留原始错误摘要，
  且仅限 `NON_RETRYABLE`/`CONTEXT_OVERFLOW` 两类已知错误。
