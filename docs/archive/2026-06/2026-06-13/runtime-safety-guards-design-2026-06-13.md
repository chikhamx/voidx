# Runtime Safety Guards 设计

> **Status: Done**
> 日期: 2026-06-13

## 1. 背景

`AgentMaxSteps` 和 `AgentDef.max_steps` 已从主代理和子代理的 agent/persona 静态配置中移除。主代理不再按固定 LLM call 上限收敛；子代理仍保留本次 `agent(..., max_steps=N)` delegation 预算，作为调用方给出的局部工作边界。移除静态预算后，runtime 仍需要防失控机制，但这些机制不应按 agent/persona 固定配置，也不应替代 workflow gate、permission sandbox 或用户中断。

当前已存在的 guard：

- 用户 interrupt：由 UI/runtime 控制，可停止当前 turn 或子代理
- context overflow / compaction：token 估算溢出时触发压缩
- tool output prune：turn 后裁剪旧工具输出，降低上下文膨胀

本设计已实现的 guard：

- tool failure loop guard
- no-progress guard
- wall-clock guard

## 2. 目标

- 防止主代理或子代理在没有有效进展时无限循环
- 将主代理“失控保护”从固定 max steps 转为运行时信号
- 让 guard 的触发原因可观察、可测试、可解释
- guard 触发后优先要求模型收敛、改用替代方案或向用户说明阻塞

## 3. 非目标

- 不恢复 `AgentMaxSteps`
- 不给主代理重新引入固定 LLM call 上限
- 不移除子代理单次 delegation 的 `max_steps` 参数
- 不绕过 workflow gate、permission sandbox、approval policy
- 不让 guard 自动决定高风险工具是否可执行

## 4. Guard 设计

| guard | 当前状态 | 触发条件 | 记录数据 | 动作 |
|-------|----------|----------|----------|------|
| interrupt | 已存在 | 用户中断运行 | UI/runtime cancellation state | 停止当前 turn 或子代理 |
| context / compaction | 已存在 | token overflow 或消息过多 | token estimate、context frame、summary | compact/prune 后继续，失败则请求用户处理 |
| tool failure loop | 已实现 | 同一 `(tool, normalized args, error kind)` 连续失败多次 | 最近失败 key、失败次数、错误摘要 | 分级注入 guidance：先提示收敛/换路/说明阻塞，第三次使用严厉措辞 |
| no-progress | 已实现 | 连续 3 次 LLM 调用没有新增 evidence / diff / todo status progress / workflow event / useful final answer | progress fingerprint、last changed source | 注入 guidance：收敛到当前事实或请求用户输入 |
| wall-clock | 已实现 | 主 turn 超过 5 分钟；子代理超过 90 秒；更高阈值可配置 | start time、last activity time、当前 tool/subagent | UI 提示状态；超过确认阈值时请求继续确认 |

## 5. Tool Failure Loop Guard

### 5.1 失败 key

失败 key 用于判断是否在重复同一失败：

```text
failure_key = (
  tool_name,
  normalized_args,
  error_kind,
)
```

`normalized_args` 应避免记录大块内容：

- `read` / `write` / `edit` / `lsp_format`：使用 `file_path`
- `grep`：使用 `pattern + include/path`
- `bash`：使用规范化 command，去掉重复空白
- `agent`：使用 `persona + delegation_reason`
- 其他工具：使用稳定 JSON 序列化后的短 hash

`error_kind` 优先从 `ToolResult.metadata["error_kind"]` 读取；各工具应逐步补齐结构化错误类型。输出文本解析只作为 fallback，避免依赖渲染文案。

归一化类型：

- `validation_error`
- `permission_denied`
- `sandbox_denied`
- `file_not_found`
- `stale_file`
- `tool_exception`
- `unknown_error`

### 5.2 状态存储

在 turn runtime 内维护：

```python
class ToolFailureLoopState(BaseModel):
    last_key: str = ""
    count: int = 0
    last_error: str = ""
    warned_count: int = 0
```

子代理也维护自己的本地状态，不写入主代理 Runtime State。

### 5.3 触发动作

同一 failure key 第一次失败只记录，不注入额外 guidance，因为工具失败本身已经会作为 tool result 回到模型。

第二次连续失败后，向下一次 LLM call 注入轻量 guidance：

```text
The same tool call has failed twice. Do not keep retrying it unchanged.
Either change the approach, summarize the blocker, or ask for the missing input.
```

第三次连续失败后，向下一次 LLM call 注入严厉 guidance：

```text
The same tool call has failed 3 times. Stop retrying it now.
Do not call this tool again with the same arguments.
Summarize the failure, choose a materially different approach if one exists,
or explain the blocker and the exact input needed from the user.
```

如果第三次 guidance 之后仍产生同一 failure key，runtime 应阻止该工具调用并返回 synthetic tool result，要求最终答复、替代方案或请求用户输入。阻止动作只针对相同 failure key，不影响其他工具或同一工具的 materially different arguments。

## 6. No-Progress Guard

### 6.1 进展指纹

每次 LLM call 和 tool execution 后计算 progress fingerprint：

```text
progress_fingerprint = hash(
  new_tool_result_ids,
  changed_file_paths,
  todo_status_progress,
  workflow_evidence_count,
  workflow_status_changes,
  nonempty_final_answer_marker,
)
```

不把 token 使用量、step number、streaming text 当作进展。

`new_tool_result_ids` 在实现中不是简单使用每次工具调用的新 id，而是使用稳定证据键：`tool_name + normalized_args + output/summary/diff hash`。同一工具、同一参数、同一输出的重复结果不算新证据；同一文件或命令产生不同输出时才算新证据。

`todo_status_progress` 只表示语义推进，例如新增必要任务、任务进入 `in_progress`、任务完成或取消。仅重写文案、重排列表、重复提交相同状态、在 todo 内部来回改措辞不算进展。

### 6.2 无进展定义

以下情况不算进展：

- 重复读取同一文件且没有使用新证据
- 重复运行同一失败命令
- 重复输出计划但没有推进 workflow 或产生可执行证据
- 子代理返回空结果或仅重复已知内容
- 同一工具连续成功调用但未产生新 evidence（见 6.3）
- todo 只发生文本改写、重排或重复提交，没有状态推进

以下情况算进展：

- 新增可引用工具结果
- 文件 diff 发生变化
- todo 出现语义状态推进
- workflow evidence/status 变化
- 模型给出可交付 final answer

### 6.3 重复工具调用检测

6.2 中"同一工具连续成功调用但未产生新 evidence"是 no-progress 的一个重要子场景，需要单独检测。

#### 6.3.1 问题场景

实测发现 GLM-5-FP8 在执行 LSP 工具合并任务时，从 step 8 到 step 103 连续调用 `todo` 工具（最长连续 91 次），每次只更新 todo 列表内容但不执行任何实际工作（read/edit/bash 等）。模型陷入了"只更新 todo 不干活"的循环。

特征：

- 连续 N 个 LLM cycle（N ≥ 2）只调用同一低价值状态工具
- 该工具调用成功（非 failure loop）
- 没有 read/edit/bash/grep/agent 等实际取证或执行工具穿插
- fingerprint 不变（todo 内容虽变但没有 todo status progress）

#### 6.3.2 检测机制

在每次 LLM cycle 的 tool execution 完成后，维护一个滑动窗口记录最近 N 个 cycle 的工具调用摘要，而不是记录扁平 tool call 序列。这样可以避免同一条 assistant message 中并行发出多个相同工具时被误判。

```python
class RepetitiveToolCycleState(BaseModel):
    recent_cycles: list[ToolCycleSummary] = []
    window_size: int = 2
    warned_tool: str = ""

    def record(self, summary: ToolCycleSummary) -> None:
        self.recent_cycles.append(summary)
        if len(self.recent_cycles) > self.window_size * 3:
            self.recent_cycles = self.recent_cycles[-self.window_size * 2:]

    def is_stuck(self) -> tuple[bool, str, int]:
        """检查是否卡在低价值单工具循环。"""
        if len(self.recent_cycles) < self.window_size:
            return False, "", 0
        window = self.recent_cycles[-self.window_size:]
        if all(item.only_tool == window[0].only_tool for item in window):
            tool = window[0].only_tool
            if tool in LOW_VALUE_REPETITIVE_TOOLS and not any(item.has_progress for item in window):
                return True, tool, len(window)
        return False, "", 0
```

检测条件：最近 `window_size`（默认 2）个 LLM cycle 都只调用同一个低价值状态工具，并且没有产生 progress fingerprint 变化。

`ToolCycleSummary` 至少包含：

- `tool_names`：本 cycle 实际执行的工具名集合
- `only_tool`：当且仅当本 cycle 只执行一种工具时记录工具名，否则为空
- `has_progress`：本 cycle 是否产生 diff、workflow evidence/status、todo status progress、新 evidence 或 final answer
- `call_count`：本 cycle 中该工具的调用次数，用于提示文案

#### 6.3.3 与 progress fingerprint 的关系

重复工具调用检测是 no-progress guard 的前置快速路径：

1. **重复工具调用检测**：纯结构化检查，只看工具名称序列，成本低，可在 tool execution 后立即判断
2. **progress fingerprint**：语义检查，需要计算 hash，在 LLM call 后判断

两者独立运行，任一触发都应注入 guidance。重复工具调用检测可以在完整 no-progress 计数之前先给轻提示，但仍需要结合本 cycle 的 progress 判断，避免误伤有效的连续读取或连续命令执行。

#### 6.3.4 触发动作

检测到低价值状态工具连续调用后，注入轻微 guidance：

```text
You have only called {tool_name} for the last {count} tool cycles.
Avoid repeating state updates. Take one concrete work action next,
or briefly explain what is blocking you.
```

如果轻提示后下一次 LLM call 仍然只调用同一低价值状态工具且没有 progress，runtime 应跳过该工具调用并返回 synthetic tool result，要求模型采取不同行动。再下一次仍重复时，设置 `should_continue = False` 终止 turn，并输出阻塞说明。

`todo` 的要求更严格：todo 工具不应被连续调用来“整理状态”。第一次连续 todo cycle 后只给轻提示；如果再次连续 todo 且没有语义状态推进，应跳过该 todo 调用，让模型执行实际工作或说明阻塞。

#### 6.3.5 豁免工具

以下工具的连续调用不应触发此检测：

- `bash`：可能需要连续运行不同命令
- `read`：可能需要连续读取不同文件
- `grep`：可能需要连续搜索不同模式

这些工具的"重复"应由 progress fingerprint 的语义检查覆盖，而非结构化快速路径。

以下工具的连续调用应触发此检测（低价值状态工具）：

- `todo`：只更新任务列表；只有语义状态推进才算 progress
- `advance_workflow`：只有产生 workflow evidence/status 变化才算 progress
- `plan_checkpoint`：只展示计划，不产生 evidence

### 6.4 触发动作

连续 3 次无进展后，注入 guidance：

```text
No meaningful progress has been detected across the last 3 model calls.
Do not start broad new exploration. Summarize what is known, state the blocker,
and either choose one concrete next action or ask the user for input.
```

如果再连续 2 次无进展，则暂停当前 turn，输出阻塞状态。

### 6.4.1 二级阈值与终止

no-progress guard 的二级阈值（连续 5 次无进展后终止 turn）应与重复工具调用检测的二级阈值（连续 2 次同一工具后终止）协调：

- 重复工具调用检测先给轻提示（结构化检查更快）
- 如果模型忽略重复工具调用 guidance，先跳过同类低价值工具调用，再终止
- 不再等待 progress fingerprint 的完整 5 次计数

## 7. Wall-Clock Guard

### 7.1 阈值

默认阈值：

- 主 turn 运行 5 分钟：UI 状态提示
- 子代理运行 90 秒：UI 状态提示
- 主 turn 运行 10 分钟：请求用户确认继续
- 子代理运行 3 分钟：要求子代理收敛并返回当前结论

阈值应作为 runtime 常量或配置，不属于 agent/persona step budget。子代理的 wall-clock guard 与 `max_steps` 并存：`max_steps` 是调用方给出的本次 delegation 预算；wall-clock 是 runtime 保护，避免子代理在单步工具或模型调用中长时间卡住。

### 7.2 UI 行为

提示示例：

```text
voidx still running (5m12s, latest action: grep src/)
Reviewer still running (94s, latest action: read tests/test_agent/test_core_flow.py)
```

超过确认阈值：

```text
This turn has been running for 10m. Continue, summarize current state, or stop?
```

实现上应优先在 graph node 边界和工具执行完成后检查确认阈值；长时间运行的单个工具/LLM call 由后台 watchdog 发 UI 状态提示，但不在调用中途强行绕过 permission/sandbox。

## 8. Context 与持久化

- guard 状态可以写入 context frame metadata，便于调试
- metadata 应记录 guard 类型、key/count、trigger level、是否已注入 guidance、是否跳过工具调用
- guard 状态不进入普通 Runtime State 文本，除非已经触发并需要模型响应
- 子代理 guard 状态不回传给主代理，除非影响最终结果
- UI 可以显示 guard 状态，因为这是运行可观测性，不是 LLM 推理上下文

## 9. 实现计划

### Task 1：Tool failure loop

文件：

- `src/voidx/agent/graph/runtime_guards.py`
- `src/voidx/agent/graph/tool_executor.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/subagent.py`
- `tests/test_agent/test_core_flow.py`
- `tests/test_agent/test_runtime_guards.py`

改动：

- 记录 failure key
- 从 `ToolResult.metadata["error_kind"]` 优先读取错误类型，输出解析作为 fallback
- 第 2 次连续失败注入轻量 guidance
- 第 3 次连续失败注入严厉 guidance
- 严厉 guidance 后再次重复时阻止同一工具调用

### Task 2：No-progress guard

文件：

- `src/voidx/agent/graph/runtime_guards.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/tool_executor.py`
- `src/voidx/agent/graph/subagent.py`
- `src/voidx/agent/graph/convergence.py`
- `tests/test_agent/test_core_flow.py`
- `tests/test_agent/test_runtime_guards.py`

改动：

- 计算 progress fingerprint
- 明确 todo 只有状态推进才算 progress
- 连续无进展后注入 guidance
- 超过二级阈值时暂停并返回阻塞说明

### Task 2a：重复工具调用检测（no-progress 子任务）

文件：

- `src/voidx/agent/graph/runtime_guards.py`
- `src/voidx/agent/graph/tool_executor.py`（记录每个 LLM cycle 的工具摘要）
- `src/voidx/agent/graph/core.py`（检测 + 注入 guidance）
- `src/voidx/agent/graph/subagent.py`（子代理同样检测）
- `src/voidx/agent/graph/convergence.py`（guidance 文本）
- `tests/test_agent/test_core_flow.py`
- `tests/test_agent/test_runtime_guards.py`

改动：

- `RepetitiveToolCycleState` 按 LLM cycle 记录滑动窗口
- tool execution 完成后记录 `ToolCycleSummary`
- `_call_llm` 前检查是否需要注入轻提示 guidance
- 轻提示后仍重复时跳过低价值工具调用；再次重复时 `should_continue = False`
- 豁免工具列表：bash、read、grep

### Task 3：Wall-clock guard

文件：

- `src/voidx/agent/graph/turn_runner.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/subagent.py`
- `src/voidx/ui/output/events/schema.py`
- `tests/test_ui_events.py`

改动：

- 记录 turn/subagent start time 和 last activity
- 超过提示阈值发 UI 状态
- 超过确认阈值请求继续或收敛

## 10. 测试矩阵

| 场景 | 预期 |
|------|------|
| 同一失败工具第 2 次出现 | 注入轻量换路 guidance |
| 同一失败工具第 3 次出现 | 注入严厉停止重复 guidance |
| 严厉 guidance 后同一失败工具再次出现 | runtime 阻止重复调用并要求收敛 |
| 连续 3 次 LLM call 无新增 evidence | 注入 no-progress guidance |
| 读取新文件或 todo 状态语义推进 | 重置 no-progress 计数 |
| todo 仅重排/改文案/重复提交 | 不重置 no-progress 计数 |
| 连续 2 个 cycle 只调用 todo 且没有状态推进 | 注入轻微重复 todo guidance |
| 重复 todo guidance 后仍只调 todo | runtime 跳过该 todo 调用 |
| 跳过后仍连续只调 todo | runtime 终止 turn 并输出阻塞说明 |
| 连续 5 次只调用 bash（豁免工具） | 不触发重复工具调用检测 |
| 主 turn 超过 5 分钟 | UI 显示 still running |
| 子代理超过 90 秒 | UI 显示子代理 still running |
| 子代理超过收敛阈值 | 要求返回当前结论 |

## 11. 成功标准

- 不恢复静态 `AgentMaxSteps`
- 子代理继续支持 `agent(..., max_steps=N)` 单次 delegation 预算
- guard 触发可在测试中稳定复现
- guard 触发原因进入 metadata / UI，可诊断
- guard guidance 不污染长期 Runtime State
- todo 连续调用只触发轻提示和低价值工具拦截，不误伤实际工作工具
