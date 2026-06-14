# Runtime Safety Guards 设计

> **Status: In Progress**
> 日期: 2026-06-13

## 1. 背景

`AgentMaxSteps` 和 `AgentDef.max_steps` 已从主代理和子代理静态预算路径中移除。移除固定步数后，runtime 仍需要防失控机制，但这些机制不应按 agent/persona 固定配置，也不应替代 workflow gate、permission sandbox 或用户中断。

当前已存在的 guard：

- 用户 interrupt：由 UI/runtime 控制，可停止当前 turn 或子代理
- context overflow / compaction：token 估算溢出时触发压缩
- tool output prune：turn 后裁剪旧工具输出，降低上下文膨胀

待实现的 guard：

- tool failure loop guard
- no-progress guard
- wall-clock guard

## 2. 目标

- 防止主代理或子代理在没有有效进展时无限循环
- 将“失控保护”从固定 max steps 转为运行时信号
- 让 guard 的触发原因可观察、可测试、可解释
- guard 触发后优先要求模型收敛、改用替代方案或向用户说明阻塞

## 3. 非目标

- 不恢复 `AgentMaxSteps`
- 不给主代理重新引入固定 LLM call 上限
- 不绕过 workflow gate、permission sandbox、approval policy
- 不让 guard 自动决定高风险工具是否可执行

## 4. Guard 设计

| guard | 当前状态 | 触发条件 | 记录数据 | 动作 |
|-------|----------|----------|----------|------|
| interrupt | 已存在 | 用户中断运行 | UI/runtime cancellation state | 停止当前 turn 或子代理 |
| context / compaction | 已存在 | token overflow 或消息过多 | token estimate、context frame、summary | compact/prune 后继续，失败则请求用户处理 |
| tool failure loop | 待实现 | 同一 `(tool, normalized args, error kind)` 连续失败 3 次 | 最近失败 key、失败次数、错误摘要 | 注入 guidance：停止重复工具、总结失败、改用替代方案或说明阻塞 |
| no-progress | 待实现 | 连续 3 次 LLM 调用没有新增 evidence / diff / todo / workflow event / useful final answer | progress fingerprint、last changed source | 注入 guidance：收敛到当前事实或请求用户输入 |
| wall-clock | 待实现 | 主 turn 超过 5 分钟；子代理超过 90 秒；更高阈值可配置 | start time、last activity time、当前 tool/subagent | UI 提示状态；超过确认阈值时请求继续确认 |

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

`error_kind` 从 `ToolResult.metadata` 或输出中归一化：

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
```

子代理也维护自己的本地状态，不写入主代理 Runtime State。

### 5.3 触发动作

连续 3 次相同 failure key 后，向下一次 LLM call 注入 guidance：

```text
The same tool call has failed 3 times. Stop retrying it.
Summarize the failure, try a materially different approach if one exists,
or explain the blocker and what input is needed.
```

如果下一次仍产生同一 failure key，runtime 应阻止该工具调用并返回 tool result，要求最终答复或请求用户输入。

## 6. No-Progress Guard

### 6.1 进展指纹

每次 LLM call 和 tool execution 后计算 progress fingerprint：

```text
progress_fingerprint = hash(
  new_tool_result_ids,
  changed_file_paths,
  todo_state_version,
  workflow_evidence_count,
  workflow_status_changes,
  nonempty_final_answer_marker,
)
```

不把 token 使用量、step number、streaming text 当作进展。

### 6.2 无进展定义

以下情况不算进展：

- 重复读取同一文件且没有使用新证据
- 重复运行同一失败命令
- 重复输出计划但没有推进 workflow/todo
- 子代理返回空结果或仅重复已知内容
- 同一工具连续成功调用但未产生新 evidence（见 6.4）

以下情况算进展：

- 新增可引用工具结果
- 文件 diff 发生变化
- todo 状态变化
- workflow evidence/status 变化
- 模型给出可交付 final answer

### 6.4 重复工具调用检测

6.2 中"同一工具连续成功调用但未产生新 evidence"是 no-progress 的一个重要子场景，需要单独检测。

#### 6.4.1 问题场景

实测发现 GLM-5-FP8 在执行 LSP 工具合并任务时，从 step 8 到 step 103 连续调用 `todo` 工具（最长连续 91 次），每次只更新 todo 列表内容但不执行任何实际工作（read/edit/bash 等）。模型陷入了"只更新 todo 不干活"的循环。

特征：

- 连续 N 步（N ≥ 5）只调用同一工具
- 该工具调用成功（非 failure loop）
- 没有其他工具调用穿插
- fingerprint 不变（todo 内容虽变但 todo_state_version 的语义变化不算进展）

#### 6.4.2 检测机制

在每次 tool execution 后，维护一个滑动窗口记录最近 N 步的工具调用序列：

```python
class RepetitiveToolCallState(BaseModel):
    recent_calls: list[str] = []  # 最近 N 步的工具名称列表
    window_size: int = 5

    def record(self, tool_names: list[str]) -> None:
        self.recent_calls.extend(tool_names)
        if len(self.recent_calls) > self.window_size * 3:
            self.recent_calls = self.recent_calls[-self.window_size * 2:]

    def is_stuck(self) -> tuple[bool, str]:
        """检查是否卡在单一工具循环。"""
        if len(self.recent_calls) < self.window_size:
            return False, ""
        window = self.recent_calls[-self.window_size:]
        unique = set(window)
        if len(unique) == 1:
            return True, window[0]
        return False, ""
```

检测条件：最近 `window_size`（默认 5）步的工具调用全部是同一工具名称。

#### 6.4.3 与 progress fingerprint 的关系

重复工具调用检测是 no-progress guard 的前置快速路径：

1. **重复工具调用检测**：纯结构化检查，只看工具名称序列，成本低，可在 tool execution 后立即判断
2. **progress fingerprint**：语义检查，需要计算 hash，在 LLM call 后判断

两者独立运行，任一触发都应注入 guidance。重复工具调用检测可以在 fingerprint 计算之前就拦截，避免浪费 LLM call。

#### 6.4.4 触发动作

检测到重复工具调用后，注入 guidance：

```text
The same tool ({tool_name}) has been called {count} times in a row without any other action.
Stop updating state and start executing actual work.
If you are stuck, explain the blocker and ask the user for input.
```

如果下一次 LLM call 仍然只调用同一工具，runtime 应跳过该工具调用并返回 tool result 提示模型必须采取不同行动，或直接 `should_continue = False` 终止 turn。

#### 6.4.5 豁免工具

以下工具的连续调用不应触发此检测：

- `bash`：可能需要连续运行不同命令
- `read`：可能需要连续读取不同文件
- `grep`：可能需要连续搜索不同模式

这些工具的"重复"应由 progress fingerprint 的语义检查覆盖，而非结构化快速路径。

以下工具的连续调用应触发此检测（低价值状态更新工具）：

- `todo`：只更新任务列表，不产生 evidence
- `advance_workflow`：只推进 workflow 状态，不产生 evidence
- `plan_checkpoint`：只展示计划，不产生 evidence

### 6.3 触发动作

连续 3 次无进展后，注入 guidance：

```text
No meaningful progress has been detected across the last 3 model calls.
Do not start broad new exploration. Summarize what is known, state the blocker,
and either choose one concrete next action or ask the user for input.
```

如果再连续 2 次无进展，则暂停当前 turn，输出阻塞状态。

### 6.3.1 二级阈值与终止

no-progress guard 的二级阈值（连续 5 次无进展后终止 turn）应与重复工具调用检测的二级阈值（连续 2 次同一工具后终止）协调：

- 重复工具调用检测先触发（结构化检查更快）
- 如果模型忽略重复工具调用 guidance，直接终止
- 不再等待 progress fingerprint 的完整 5 次计数

## 7. Wall-Clock Guard

### 7.1 阈值

默认阈值：

- 主 turn 运行 5 分钟：UI 状态提示
- 子代理运行 90 秒：UI 状态提示
- 主 turn 运行 10 分钟：请求用户确认继续
- 子代理运行 3 分钟：要求子代理收敛并返回当前结论

阈值应作为 runtime 常量或配置，不属于 agent/persona step budget。

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

## 8. Context 与持久化

- guard 状态可以写入 context frame metadata，便于调试
- guard 状态不进入普通 Runtime State 文本，除非已经触发并需要模型响应
- 子代理 guard 状态不回传给主代理，除非影响最终结果
- UI 可以显示 guard 状态，因为这是运行可观测性，不是 LLM 推理上下文

## 9. 实现计划

### Task 1：Tool failure loop

文件：

- `src/voidx/agent/graph/tool_executor.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/subagent.py`
- `tests/test_agent/test_core_flow.py`

改动：

- 记录 failure key
- 连续 3 次失败后注入 guidance
- 再次重复时阻止同一工具调用

### Task 2：No-progress guard

文件：

- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/tool_executor.py`
- `src/voidx/agent/graph/subagent.py`
- `src/voidx/agent/graph/convergence.py`
- `tests/test_agent/test_core_flow.py`

改动：

- 计算 progress fingerprint
- 连续无进展后注入 guidance
- 超过二级阈值时暂停并返回阻塞说明

### Task 2a：重复工具调用检测（no-progress 子任务）

文件：

- `src/voidx/agent/graph/tool_executor.py`（记录工具调用序列）
- `src/voidx/agent/graph/core.py`（检测 + 注入 guidance）
- `src/voidx/agent/graph/subagent.py`（子代理同样检测）
- `src/voidx/agent/graph/convergence.py`（guidance 文本）
- `tests/test_agent/test_core_flow.py`

改动：

- `RepetitiveToolCallState` 滑动窗口
- tool execution 后调用 `record()`
- `_call_llm` 前调用 `is_stuck()`，触发时注入 guidance
- 二级触发时跳过工具调用或 `should_continue = False`
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
| 同一 read/file_not_found 连续失败 3 次 | 注入停止重复 guidance |
| 同一失败工具第 4 次出现 | runtime 阻止重复调用并要求收敛 |
| 连续 3 次 LLM call 无新增 evidence | 注入 no-progress guidance |
| 读取新文件或更新 todo | 重置 no-progress 计数 |
| 连续 5 次只调用 todo 工具 | 注入重复工具调用 guidance |
| 重复工具调用 guidance 后仍只调 todo | runtime 终止 turn 或跳过该工具调用 |
| 连续 5 次只调用 bash（豁免工具） | 不触发重复工具调用检测 |
| 主 turn 超过 5 分钟 | UI 显示 still running |
| 子代理超过 90 秒 | UI 显示子代理 still running |
| 子代理超过收敛阈值 | 要求返回当前结论 |

## 11. 成功标准

- 不恢复静态 `AgentMaxSteps`
- guard 触发可在测试中稳定复现
- guard 触发原因进入 metadata / UI，可诊断
- guard guidance 不污染长期 Runtime State
- 子代理仍由 `agent(..., max_steps=N)` 控制本次 delegation 预算
