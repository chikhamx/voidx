# Agent Budget Convergence & Subagent Failure Recovery

> **Status: Done** — Archived on 2026-08-10.

Date: 2026-08-09
Revised: 2026-08-10

> 主、子 agent 共用无 I/O 的预算信号内核，但由角色策略解释信号。子 agent 对
> step、wall-clock、context 进行 soft guidance 和 hard 无工具最终总结；主 agent
> 本期只把现有 context pressure 接入共享内核，继续 compaction 优先、保留完整工具，
> 并由自己结束 turn。

## 1. Decisions

1. `budget_convergence.py` 只比较已启用的预算读数，返回 `none`、`soft` 或 `hard`
   信号及内部 metadata；不知道主/子角色，也不决定 prompt、工具面、final call、
   compaction、finish reason 或 turn 结束。
2. `subagent_convergence.py` 是子 agent 策略：`soft -> guide`，`hard -> finalize`；
   hard 使用一次无工具最终总结。
3. `context_pressure.py` 是主 agent 策略：使用现有 compaction soft/hard 阈值构造
   context reading；可 compact 时不注入提示，不可 compact 时把 soft/hard 映射到
   现有 context-pressure hint。
4. 主 agent hard context 始终保留完整工具，不设置 `convergence_forced`，不自动调用
   `turn(stop)`，也不执行专用无工具 final call；主 agent 自己结束 turn。
5. 本期不给主 agent 新增 step 或 wall-clock 预算。LangGraph recursion limit 2000
   继续只是安全 guard，不作为业务预算。
6. 子 agent 默认硬预算保持 100 个常规模型步骤和 1800 秒；step/time 在 80% 进入
   soft，达到 hard 时进入一次无工具最终总结。
7. 子 agent context 默认在 75% soft、90% hard；阈值按子 agent 实际模型计算。
8. 子 agent guidance 不暴露 runtime、预算、时间、步骤或上下文触发原因；共享信号
   metadata 不进入 prompt。主 agent 保留现有明确的 context-pressure 文案。
9. `CONTEXT_OVERFLOW` 和可识别的非重试 LLM 错误在子 LLM 调用边界恢复已有结果；
   runtime/config/programming 异常继续抛出。
10. 不自动重试所有 timeout 工具。只有未来显式声明 `retry_safe` 的工具才可进入
    自动重试设计。
11. inbox 容量和 `progress` 消息移除由
    `docs/archive/subagent-inbox-pressure-2026-08-08.md` 定义。

## 2. Current Problems

| 行为 | 原实现 | 问题 |
| --- | --- | --- |
| 子 agent 步骤上限 | `subagent.py` 的 `_SAFETY_STEP_LIMIT = 100` | 到点直接返回最后一条消息，没有最终总结机会 |
| 子 agent 墙钟上限 | `WallClockGuardState.for_subagent()` 固定 1800 秒 | 只在工具周期后硬终止，之前没有预算提示 |
| 子 agent 上下文 | 每步估算 `context_tokens` | 只记账，不参与收敛决策 |
| 主 agent context pressure | `context_pressure.py` 独立比较 soft/hard | 与子 agent 形成第二套阈值分类逻辑 |
| provider overflow | `CONTEXT_OVERFLOW` 直接 raise | 已有 child 发现随失败丢失 |
| 配置 | `Config` 无子 agent 预算对象 | 无法通过 workspace settings 调整预算 |
| 错误恢复 | `run_subagent()` 外层统一 raise | 已有模型输出无法作为部分结果返回 |

## 3. Goals

- 主、子 agent 的预算阈值分类共用同一纯信号内核。
- 角色策略独立解释信号，不在共享模块堆叠 `if main/child` 分支。
- 子 agent 的 step/time/context 检测共用一个状态机，hard 时获得一次最终总结机会。
- 30 分钟子 agent 时间预算触发收敛，而不是直接返回 runtime guard 文本。
- 主 agent 保留现有 compaction、provider overflow、完整工具和自主结束 turn 语义。
- 子 agent 的预算可通过 workspace settings 配置。
- provider 拒绝后保留已有 child 发现，父 agent 能识别结果完整性。

## 4. Non-Goals

- 不给主 agent 新增 step/time 预算、配置或提示。
- 不让主 agent hard context 进入子 agent 的无工具 final call。
- 不改变主 agent context-pressure marker、compaction 阈值、provider overflow 恢复或
  `convergence_forced` 语义。
- 不给子 agent 引入 compaction。
- 不重做 workflow DAG 或结构化终止汇报协议。
- 不提供通用 timeout 工具重试。
- 不保证 hard 发生时立即抢占正在执行的 LLM/tool；动作只发生在安全边界。
- 不把子 agent 的预算触发原因写入 LLM guidance 或用户可见最终文本。

## 5. Shared Signal Core and Role Policies

### 5.1 Shared Types

`src/voidx/agent/adapters/langgraph/runtime/budget_convergence.py` 是无 I/O 的纯模块，
不依赖 LangGraph、gateway、UI、模型或角色。

```python
BudgetDimension = Literal["step", "wall_clock", "context"]
ConvergenceLevel = Literal["none", "soft", "hard"]

@dataclass(frozen=True)
class BudgetReading:
    dimension: BudgetDimension
    current: float
    soft_limit: float
    hard_limit: float

@dataclass(frozen=True)
class ConvergenceDecision:
    triggered_dimensions: frozenset[BudgetDimension]
    level: ConvergenceLevel
    metadata: dict[str, float | str]

@dataclass
class BudgetConvergenceState:
    soft_prompted: bool = False
    hard_prompted: bool = False
```

`triggered_dimensions` 和 `metadata` 只用于策略、追踪和测试。共享模块没有 action 或
prompt renderer，避免把角色动作或触发原因泄漏进内核。

### 5.2 Signal Rules

`decide_convergence(readings, state)` 按以下顺序决策：

1. 任一已启用维度达到 hard，且尚未发出 hard：返回 `hard`。
2. 否则任一维度达到 soft，且尚未发出 soft：返回 `soft`。
3. 否则返回 `none`。

约束：

- 一个状态最多发出一次 soft 和一次 hard。
- 多维同时越界只产生一个信号；全部触发维度保留在内部 metadata。
- hard 优先于 soft；首次采样已到 hard 时不先发 soft。
- adapter 只传入已启用维度；例如 `context_limit <= 0` 时传空 readings。
- 内核不决定 compaction、错误恢复、finish reason、工具面或是否调用模型。

### 5.3 Child Policy

`src/voidx/agent/adapters/langgraph/runtime/subagent_convergence.py` 定义：

| signal | child action |
| --- | --- |
| `none` | `continue` |
| `soft` | `guide` |
| `hard` | `finalize` |

子 agent 专属 guidance 常量和中英文渲染也归该模块所有。提示不能包含动态原因、
数值或 runtime 术语：

```text
SOFT_CONVERGENCE_GUIDANCE:
请停止扩展范围，完成当前目标，并尽快给出简洁、完整的结果总结。

FINAL_CONVERGENCE_GUIDANCE:
请不要再调用工具。基于已有信息完成当前任务，并立即输出最终结果；
如尚未全部完成，请明确已有结论、验证情况和剩余项。
```

英文语言配置使用等价英文常量。提示通过现有 marker 标记为 guidance，但 marker
本身不出现在消息正文。

### 5.4 Main Policy

`src/voidx/agent/adapters/langgraph/runtime/context_pressure.py` 使用：

- `current = llm_context_tokens`；
- `soft_limit = CompactionService.soft_threshold()`；
- `hard_limit = context_limit * 0.90`。

共享信号只替换原来的 soft/hard 分类。`can_compact`、turn selection、stable pressure
ID、soft-to-hard upgrade 和 UI events 仍由 context-pressure 策略负责。

| 条件 | main action |
| --- | --- |
| `none` | 保持正常循环 |
| `soft/hard` 且 `can_compact=True` | 优先走现有 compaction，不注入预算提示 |
| `soft` 且不可 compact | 注入现有 soft context-pressure hint |
| `hard` 且不可 compact | 升级为现有 hard hint，保留完整工具并继续正常循环 |

主 agent 不消费 child 的 `finalize` action 或 child guidance。达到 hard 后仍由模型按
现有 turn protocol 正常调用 `turn(stop)` 或自然结束。

## 6. Configuration

### 6.1 Model

在 `src/voidx/config/models.py` 增加：

```python
class SubagentBudgetConfig(BaseModel):
    step_limit: int = Field(default=100, ge=1, le=1000)
    wall_clock_seconds: float = Field(
        default=1800.0,
        ge=1.0,
        le=86400.0,
        allow_inf_nan=False,
    )
    soft_warn_ratio: float = Field(default=0.80, ge=0.1, le=0.95)
    context_soft_ratio: float = Field(default=0.75, ge=0.1, le=0.90)
    context_hard_ratio: float = Field(default=0.90, ge=0.5, le=0.98)
```

并在 `Config` 增加：

```python
subagent_budget: SubagentBudgetConfig = Field(default_factory=SubagentBudgetConfig)
```

增加 model validator，要求 `context_soft_ratio < context_hard_ratio`。

### 6.2 Settings Wiring

配置是 workspace-scoped，JSON key 为 `subagent_budget`：

```json
{
  "subagent_budget": {
    "step_limit": 100,
    "wall_clock_seconds": 1800.0,
    "soft_warn_ratio": 0.8,
    "context_soft_ratio": 0.75,
    "context_hard_ratio": 0.9
  }
}
```

变更：

- `src/voidx/config/settings.py`
  - 将 `subagent_budget` 加入 `WORKSPACE_ONLY_KEYS`。
  - `Settings.build_config()` 传入 `subagent_budget=self.get_subagent_budget_config()`。
- `src/voidx/config/settings_agent.py`
  - 实现 `get_subagent_budget_config()` 和 `set_subagent_budget_config()`。
  - 非对象或校验失败时记录 warning 并回退默认值。
- `src/voidx/config/__init__.py`
  - 导出 `SubagentBudgetConfig`。
- `subagent.py` 的 `SubagentConfig` Protocol 增加 `subagent_budget`。
- 更新 config contract snapshot。

仅在 `Config` 增字段而不接 `Settings.build_config()` 不算完成配置化。

## 7. Child Agent Convergence

### 7.1 Budget Readings

`run_subagent()` 使用最终 `model_cfg` 构造子 agent 独立预算：

| 维度 | soft limit | hard limit |
| --- | --- | --- |
| step | `step_limit * soft_warn_ratio` | `step_limit` |
| wall clock | `wall_clock_seconds * soft_warn_ratio` | `wall_clock_seconds` |
| context | `context_limit * context_soft_ratio` | `context_limit * context_hard_ratio` |

上下文限制必须按子 agent 最终 provider/protocol/context window 解析，不能直接使用
父 agent 的共享 `UsageStats.context_limit`。全局 usage stats 仍可累计 token 用量，但
不能作为子模型阈值的唯一来源。

步骤语义：

- `step_limit` 限制常规模型调用次数。
- soft 检查发生在每次常规模型调用前。
- 若第 `step_limit` 次调用已经自然输出最终答案，直接完成。
- 若第 `step_limit` 次调用仍请求工具，工具完成后不再开始第
  `step_limit + 1` 次常规调用，而是进入一次专用 final call。
- 专用 final call 不计入常规 step limit，且最多一次。

墙钟语义：

- timer 在 child run 初始化后使用 `time.monotonic()` 启动。
- 默认约 24 分钟首次 soft guidance。
- 达到 30 分钟后，在下一个安全边界进入 final call。
- 不抢占正在执行的模型调用或工具；工具自身 timeout 仍由工具层负责。

### 7.2 Loop Integration

在每次常规模型调用前：

1. 估算即将发送的 context tokens。
2. 构造启用的 step/time/context readings。
3. 调用 `decide_convergence()`。
4. `guide`：将通用 soft guidance 只加入本次 LLM 请求，不写入 `sub_messages`。
5. `finalize`：跳转到专用 final call。
6. `continue`：保持现有模型/工具循环。

现有 no-progress 和 repeated-tool guards 继续保留。它们触发终止时也应复用专用
final call，而不是把 `Runtime guard stopped...` 作为最终结果正文。guard 原因只
记录到内部 metadata。

### 7.3 Final Call

专用 final call：

- 使用原始未 bind tools 的 model，工具定义为空。
- 追加 `FINAL_CONVERGENCE_GUIDANCE`。
- 最多调用一次，不进入普通 LLM retry 循环之外的新 agent loop。
- 模型若仍返回结构化 tool calls，不执行；只提取文本。
- 文本为空或调用失败时，使用 `_partial_result_from_messages()` 从最近最多三条
  assistant 消息提取已有发现，并附通用的“未全部完成”说明。
- 最终文本不得包含 budget dimension、阈值、墙钟、context pressure、guard
  或 runtime termination 原因。

内部 finish reason：

| 触发 | finish reason |
| --- | --- |
| step hard limit | `step_limit` |
| wall-clock hard limit | `time_limit` |
| context hard limit/provider overflow | `context_limit` |
| no-progress/repeated-tool hard guard | `guard_terminated` |
| 可识别 LLM 错误恢复 | `error_recovered` |

finish reason 继续通过 run metadata 供父层判断结果完整性，但不拼入 child 的
最终自然语言总结。

### 7.4 Provider Overflow

若 provider 已返回 `CONTEXT_OVERFLOW`：

- 不再发送同等大小的 final LLM 请求。
- 从已有 assistant 消息构造 partial result。
- `report_result(..., finish_reason="context_limit")`。
- tracker 以 completed 结束，结果质量由父层标为 incomplete execution。

soft/hard context 检测应尽量避免进入该路径，但 provider 的 tokenizer 差异仍可能
导致估算低于真实请求大小。

### 7.5 LLM Error Recovery Boundary

恢复逻辑只放在 `stream_llm()` 调用的 `except` 内：

- `CONTEXT_OVERFLOW`：按 7.4 返回部分结果。
- `NON_RETRYABLE`：保留已有 assistant 文本并返回 `error_recovered`；错误摘要写入
  structured metadata/日志，不伪装成任务结论。
- 网络、rate limit、server error：维持现有有限重试。
- 重试耗尽：若已有有效 assistant 文本，返回 `error_recovered`；否则继续抛出，
  gateway 将 run 标为 failed。

`run_subagent()` 最外层 `except Exception` 不调用 `_classify_llm_error()`，避免把
配置、gateway、UI、文件系统或 programming error 误分类为 LLM 恢复。

## 8. Main Agent Invariants

主 agent 接入共享信号时必须保持：

- `context_pressure.py` 的 soft/hard 阈值、stable ID、marker 和 UI event 契约不变；
- `llm_turn.py` 继续先评估可压缩性，并在 hard overflow 时优先执行现有 compaction；
- hard hint 下仍向模型绑定完整 tool definitions；
- 不新增主 agent step/time budget，不把 recursion limit 当业务预算；
- 不设置 `convergence_forced=True`，不调用 child final policy，不自动结束 turn；
- provider overflow、deterministic fallback summary 和正常 turn protocol 保持现状。

## 9. Result Delivery

所有 child soft/hard 终止路径使用现有 `report_result()` 和 `run_metadata` 通道。
`AgentRun.result` 是父层 `agent_control(wait)` 的权威结果。

终态 result 不能因为父 inbox 已满而把已完成的 child 改成 failed；具体队列优先级
和容量规则见 inbox pressure 设计。

父层继续按任意非标准 finish reason 返回：

```text
result_quality = incomplete_execution
```

由父 agent 判断部分结果是否足够，或启动更窄的替代任务。主 agent context-pressure
只影响当前 turn 的提示与 UI 生命周期，不使用 child run finish reason。

## 10. Files

| 文件 | 变更 |
| --- | --- |
| `src/voidx/agent/adapters/langgraph/runtime/budget_convergence.py` | 主/子共用的纯 `none/soft/hard` 信号内核 |
| `src/voidx/agent/adapters/langgraph/runtime/subagent_convergence.py` | child action 映射与专属 guidance |
| `src/voidx/agent/adapters/langgraph/runtime/context_pressure.py` | main context reading 与信号策略适配 |
| `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | 子预算采样、策略执行、无工具 final、LLM 边界恢复 |
| `src/voidx/agent/adapters/langgraph/runtime/runtime_guards.py` | `for_subagent(limit_seconds=...)` 参数化；保留内部 guard metadata |
| `src/voidx/config/models.py` | 新增 `SubagentBudgetConfig` 和 `Config.subagent_budget` |
| `src/voidx/config/settings_agent.py` | workspace budget get/set |
| `src/voidx/config/settings.py` | settings scope 与 `build_config()` 接线 |
| `src/voidx/config/__init__.py` | 导出配置模型 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_budget_convergence.py` | 纯信号单测 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_convergence.py` | child policy 与 guidance 单测 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_context_pressure.py` | main adapter、compaction 优先和 marker 单测 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction.py` | main hard context 保留工具并自主结束的集成测试 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget.py` | child step/time/context soft/hard 收敛 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget_final.py` | child 无工具 final 与步骤边界 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_llm_retry.py` | overflow 和 LLM error recovery |
| `src/tests/test_config/test_config.py` | settings round-trip 和非法配置回退 |
| `src/tests/fixtures/contracts/config.json` | config contract fixture |

## 11. Tests and Acceptance Criteria

### 11.1 Focused Commands

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_budget_convergence.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_subagent_convergence.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_context_pressure.py
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction.py \
  -k "main_agent_hard_context_pressure"
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget_final.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_subagent_llm_retry.py
./test.py --backend -- src/tests/test_config/test_config.py src/tests/test_contracts/test_config_contract.py
```

### 11.2 Regression Commands

```bash
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime
./test.py --backend
```

### 11.3 Required Assertions

- 共享内核只返回 `none/soft/hard` 信号，没有角色 action 或 prompt。
- 默认 child 配置为 100 steps、1800s、step/time soft 80%、context soft 75%、hard 90%。
- settings JSON 能覆盖全部 child 预算字段，非法值回退默认并记录 warning。
- child soft/final guidance 不包含触发维度、runtime、budget、time、step、context、token、
  threshold、guard 等原因词。
- 多维同时越界只产生一个 soft/hard 信号。
- 24 分钟附近 child soft guidance；30 分钟后的安全边界只执行一次无工具 final call。
- child step limit 后不执行更多业务工具。
- main context `can_compact=True` 时不注入 pressure hint，并优先执行现有 compaction。
- main context soft/hard 使用共享信号分类，`context_limit <= 0` 时保持禁用。
- main hard context 仍绑定完整工具、只注入 hard pressure hint、`convergence_forced=False`。
- main 不新增 step/time budget，不自动 stop，不进入 child final call。
- provider overflow 不重复发送同等大小请求，并保留已有 assistant 发现。
- runtime/config/programming error 仍使 child run failed，不被标为 `error_recovered`。
- timeout 工具不会因本设计被自动重放。
- 所有 child 非标准 finish reason 在父层标记为 incomplete execution。


### 11.4 Verification Record

2026-08-10：

- 收敛、context pressure、main compaction、错误恢复与 gateway 聚焦测试：PASS。
- `src/tests/test_agent/adapters/langgraph/runtime`、subagent gateway、agent-control 与配置回归：PASS。
- `./test.py --backend`：`4565 passed, 30 skipped, 7 warnings`。
- `git diff --check` 与关键模块 compileall：PASS。
- `budget_convergence.py`、`subagent_convergence.py`、`context_pressure.py`、
  `llm_turn.py`、`subagent.py`、`inprocess_gateway.py`：无 LSP diagnostics。
- 独立静态复核 PASS：确认 wall-clock post-LLM/post-tool hard final、soft guidance
  token/frame/usage、UNKNOWN fail-fast、main hard compaction fallback、provider-overflow
  retry frame，以及 main/child 角色不变量均无剩余 P0/P1/P2。

## 12. Risks

- hard final call 会额外消耗一次模型请求，但相比直接截断能保留更完整结果。
- 30 分钟是开始最终收敛的安全边界，不是包含 final call 的绝对运行截止时间。
- context token 估算可能与 provider 不一致，因此仍需要 overflow partial fallback。
- 通用 guidance 不告诉模型具体原因，诊断只能依赖内部 metadata 和日志；这是刻意的
  用户体验和 prompt 隔离取舍。
- 默认开启 soft guidance 会改变接近预算时的模型输入；“默认行为不变”只适用于硬
  预算数值，不适用于预算附近的模型输出。
