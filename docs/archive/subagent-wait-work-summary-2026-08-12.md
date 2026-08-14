# 子代理等待超时：抽象进度与活动度建议

> **Status: Done** — Archived on 2026-08-13.

## 状态

- 日期：2026-08-12
- 状态：设计已确认，待实现
- 受众：维护者与实现者（human + LLM）
- 目标：`agent_control(action="wait")` 超时后，只向主 agent 暴露抽象进度、当前状态、最近活动和可操作的等待/中断建议。

## 1. 已确认决策

1. 单次 `wait` 默认上限保持 `256` 秒。
2. 等待超时不取消子代理；返回时生命周期 `status` 仍为 `running`。
3. 删除可由 `status` 推导的顶层 `terminal`。
4. 将 `wait_outcome="timed_out_still_running"` 简化为 `wait_outcome="timed_out"`。
5. 主 agent 不接收具体工具调用分布、具体工具名、命令或文件路径。
6. 进度只显示抽象工作量：读取文件、编辑文件、运行命令、搜索和其他操作。
7. 当前状态和最近活动只使用固定抽象类别：
   - `thinking`
   - `reading`
   - `editing`
   - `running_command`
   - `searching`
   - `other`
8. `next_step_hint` 使用真实可观测活动时间：
   - 最近 `256` 秒内观察到活动：建议继续等待；
   - 超过 `256` 秒没有观察到活动：建议中断子代理，除非该耗时符合预期。
9. 不发送人工定时心跳；长时间无流式输出的模型或工具允许被识别为“无可观测活动”。
10. 活动判断是启发式信号，文案必须使用 `observed activity`，不得宣称子代理一定卡死。

## 2. 当前差距

### 2.1 具体工具信息暴露过多

`src/voidx/agent/domain/subagent.py` 的 `AgentRun` 当前保存 `active_tools` 和 `last_tool`；`src/voidx/agent/application/subagent_status.py` 直接渲染具体工具名：

```text
Status: elapsed 4m32s · active: search (3s)
Status: elapsed 4m32s · last: read succeeded 14s ago
```

`src/voidx/agent/adapters/tools/subagent_control.py` 还会把完整 `AgentRun.model_dump()` 放入 ToolResult metadata，因此主 agent 可看到具体工具名和调用 ID。

目标行为必须改成抽象类别，不能只改文本而保留 metadata 泄漏。

### 2.2 `updated_at` 不能代表完整活动度

当前 `AgentRun.updated_at` 主要在以下时机更新：

- 工具开始；
- 工具结束；
- 生命周期结束。

子代理模型流式调用没有刷新运行记录。直接使用 `updated_at` 会把长时间模型推理误判为无活动。因此本设计新增独立 `last_activity_at`，并在模型调用开始和每个流式 chunk 刷新。

### 2.3 状态重复

等待超时目前同时返回：

```json
{
  "status": "running",
  "wait_outcome": "timed_out_still_running",
  "terminal": false
}
```

`still_running` 和 `terminal=false` 都与 `status=running` 重复。

## 3. 目标与非目标

### 3.1 目标

1. 给主 agent 一个短、稳定、抽象且可机器读取的进度快照。
2. 显示一个当前运行状态和一个最近完成活动，不暴露具体工具名。
3. 用统一活动时间判断下一步应继续等待还是考虑中断。
4. 模型和工具活动使用同一套分类与时间语义。
5. 文件计数按规范化路径去重，命令/搜索/其他按抽象操作次数累计。
6. 重复或乱序活动通知不重复累计。
7. 保持子代理生命周期、结果、取消和错误语义不变。

### 3.2 非目标

1. 不展示总工具调用数或每个具体工具的调用次数。
2. 不解析 Bash 字符串估算 shell 子命令数量。
3. 不推断 Bash、LSP、search、find 或 MCP 内部读写了哪些文件。
4. 不把命令文本、文件路径、工具参数或 tool call ID 暴露给主 agent。
5. 不在 `wait` 时回扫 JSONL、transcript 或 UI 事件重建统计。
6. 不用定时心跳证明一个无输出调用仍健康。
7. 不自动执行 cancel；只通过 `next_step_hint` 给出建议。
8. 不改变 compaction timeout、fallback 或模型配置。
9. 不新增 settings 开关或持久化迁移。

## 4. 主 agent 可见契约

### 4.1 文本输出

最近 256 秒内有活动：

```text
Helix [running]
Wait timed out after 256s.
Status: elapsed 4m32s
Progress: read 7 files · edited 3 files · ran 4 commands · searched 6 times
Current: searching · activity 12s ago
Recent: editing · succeeded 21s ago
```

超过 256 秒没有活动：

```text
Helix [running]
Wait timed out after 256s.
Status: elapsed 8m04s
Progress: read 7 files · edited 3 files · ran 4 commands · searched 6 times
Current: searching · activity 312s ago
Recent: editing · succeeded 321s ago
```

没有累计进度时不输出空的 `Progress:` 行；没有最近完成活动时不输出 `Recent:` 行。

### 4.2 `next_step_hint`

单个子代理，本次等待期间观察到活动：

```text
Activity was observed 12s ago; current state is searching. Wait again if the result is still needed.
```

单个子代理，本次等待期间没有活动：

```text
No activity was observed during the 256s wait; current state is searching and its last activity was 312s ago. Cancel the child agent unless this duration is expected.
```

规则：

- `_execute_one()` 在调用 transport wait 前记录 `wait_started_at`，返回后记录同一次 `sampled_at`。
- 比较使用 `last_activity_at >= wait_started_at`，精确判断本次等待期间是否观察到活动；默认等待窗口为 `256` 秒。
- `last_activity_at` 缺失或早于 `wait_started_at` 时按“本次等待无活动”处理，不假设任务健康。
- activity age 只用于说明距离最近活动多久，不决定 recommendation。
- 文案把“最近活动时间”和“当前状态”分开，不把某个并发活动错误归因给 current category。
- `next_step_hint` 只建议，不自动调用 `cancel`。

批量等待时，每个 timeout 子代理各产生一行建议，并带显示名：

```text
Helix: activity was observed 12s ago; current state is searching. Wait again if the result is still needed.
Orion: no activity was observed during the 256s wait; current state is thinking and its last activity was 312s ago. Cancel the child agent unless this duration is expected.
```

错误、失败和不完整结果的现有 hint 继续追加并去重。

### 4.3 结构化 metadata

目标 metadata：

```json
{
  "status": "running",
  "wait_outcome": "timed_out",
  "wait_timeout_seconds": 256,
  "result_quality": "not_available",
  "finish_reason": "",
  "run": {
    "run_id": "run_...",
    "status": "running",
    "progress": {
      "files_read": 7,
      "files_edited": 3,
      "commands_run": 4,
      "searches": 6,
      "other_actions": 2
    },
    "current_activity": {
      "category": "searching",
      "status": "running",
      "started_at": 1786492788,
      "last_observed_at": 1786492800
    },
    "recent_activity": {
      "category": "editing",
      "status": "succeeded",
      "started_at": 1786492760,
      "last_observed_at": 1786492791,
      "finished_at": 1786492791
    },
    "last_activity_at": 1786492800
  }
}
```

主 agent 可见 `run` 不得包含：

- `active_tools`
- `last_tool`
- `tool_counts`
- `tool_name`
- `tool_call_id`
- 工具参数
- 文件路径集合
- 命令文本

`run.result`、`run.error` 和现有非工具生命周期字段保持原行为。

## 5. 领域模型

在 `src/voidx/agent/domain/subagent.py` 新增：

```python
AgentActivityCategory = Literal[
    "thinking",
    "reading",
    "editing",
    "running_command",
    "searching",
    "other",
]

AgentActivityStatus = Literal["running", "succeeded", "failed"]

class AgentProgress(BaseModel):
    files_read: int = 0
    files_edited: int = 0
    commands_run: int = 0
    searches: int = 0
    other_actions: int = 0

class AgentActivity(BaseModel):
    category: AgentActivityCategory
    status: AgentActivityStatus
    started_at: float
    last_observed_at: float
    finished_at: float | None = None
```

`AgentRun` 新增：

```python
progress: AgentProgress = Field(default_factory=AgentProgress)
current_activity: AgentActivity | None = None
recent_activity: AgentActivity | None = None
last_activity_at: float | None = None
```

`current_activity` 在创建 run 时初始化为：

```python
AgentActivity(
    category="other",
    status="running",
    started_at=created_at,
    last_observed_at=created_at,
)
```

网关创建真实 run 时显式初始化该占位活动；字段保持可选默认值，以兼容现有独立 `AgentRun` 构造点。当没有模型或工具活动正在执行时，网关把 `current_activity` 设为 `other/running`，其时间取最近一次活动时间；这表示当前没有更具体的可观测阶段，不增加 `other_actions`。终态 run 可将 `current_activity` 设为 `None`。

`AgentWaitOutcome` 改为：

```python
AgentWaitOutcome = Literal[
    "already_terminal",
    "terminal_reached_during_wait",
    "timed_out",
]
```

不变量：

```text
all progress counters >= 0
last_activity_at is None or last_activity_at >= created_at
current_activity is None or current_activity.last_observed_at <= last_activity_at
recent_activity is None or recent_activity.finished_at is not None
```

## 6. 内部运行状态与隐私边界

`src/voidx/agent/adapters/subagent/inprocess_gateway.py` 的 `_RunRecord` 保留或新增仅供适配器使用的私有状态：

```python
seen_activity_ids: set[str]
finished_activity_ids: set[str]
active_activities: dict[str, InternalActivity]
pending_file_impacts: dict[str, FileImpact]
read_paths: set[str]
edited_paths: set[str]
```

内部对象可以保存具体工具名、调用 ID、参数衍生的路径和活动类别，但这些数据不得进入主 agent 可见快照。

在 `src/voidx/agent/application/subagent_status.py` 提供单一公开快照入口：

```python
def public_child_run_snapshot(run: AgentRun) -> dict: ...
```

它以 allowlist 方式构造主 agent 可见字段，而不是先完整 dump 再逐个删除。`subagent_control.py` 的单个和 batch wait 都必须调用此函数；不得依赖调用者记住删除敏感键。allowlist 保留现有生命周期与结果字段，并加入 `progress/current_activity/recent_activity/last_activity_at`；排除 `active_tools/last_tool/wait_outcome` 以及任何内部集合。顶层 `wait_outcome` 仍由 control item 单独返回。

现有 `AgentRun.active_tools` / `last_tool` 可以继续作为进程内兼容字段，但：

1. `render_child_run_metrics()` 不再渲染具体工具名；
2. `agent_control` 的 `run` metadata 必须移除这两个字段；
3. 新增抽象状态不得从对外序列化后的具体字段反推。

## 7. 抽象分类规则

### 7.1 模型活动

| 活动 | 类别 |
|---|---|
| 子代理 LLM 调用开始、流式 chunk、调用结束 | `thinking` |

模型活动不增加 `progress` 计数；只更新当前/最近活动与 `last_activity_at`。

### 7.2 工具活动

| 工具 | 抽象类别 | 进度累计 |
|---|---|---|
| `read` | `reading` | 成功后按唯一文件增加 `files_read` |
| `write`, `replace`, `manage` | `editing` | 成功修改 file 后按唯一目标文件增加 `files_edited` |
| `bash` | `running_command` | 首次 start 时 `commands_run += 1` |
| `find`, `search`, `lsp`, `websearch`, `webfetch` | `searching` | 首次 start 时 `searches += 1` |
| 其他工具 | `other` | 首次 start 时 `other_actions += 1` |

说明：

- `manage kind=dir` 当前类别仍是 `editing`，但不增加 `files_edited`。
- `manage kind=file, op=create/delete` 使用 `paths`。
- `manage kind=file, op=move` 只按 `moves[].dest` 计唯一编辑文件。
- Bash 不解析脚本内部命令；一次 Bash 工具调用计一次 `commands_run`。
- search/find/lsp 等只计抽象搜索次数，不推断其访问文件。
- 失败的 read/write/replace/manage 不合并文件集合。
- 命令、搜索和其他操作按首次启动次数累计，即使最终失败也代表已尝试工作。

### 7.3 当前活动选择

子代理工具可以并发。主 agent 只看到一个当前状态：

1. 从仍在运行的模型/工具活动中，选择 `last_observed_at` 最新者；
2. 相同时间时，选择 `started_at` 最新者；
3. 没有活动在运行时，使用 `other/running` 占位；
4. 完成活动写入 `recent_activity`，状态为 `succeeded` 或 `failed`。

不得把多个具体工具或多个类别列表暴露给主 agent。

## 8. 活动时间语义

### 8.1 会刷新 `last_activity_at` 的事件

1. 子代理 run 创建；
2. LLM attempt 开始；
3. 每个原始 LLM stream chunk；
4. 工具开始；
5. 工具结束；
6. 子代理生命周期结束。

### 8.2 不会刷新活动时间的事件

1. `agent_control.wait` 自身开始或超时；
2. 定时器或人工 heartbeat；
3. 仅仅因为 asyncio task 仍存在；
4. 主 agent 查询 run 快照；
5. UI 重绘。

这样可以避免“任务仍标记 running”被错误当作持续进展。

### 8.3 模型流式活动接入

`src/voidx/agent/adapters/langgraph/runtime/streaming.py` 的 `stream_llm()` 新增可选只读回调：

```python
on_activity: Callable[[], None] | None = None
```

行为：

- 调用模型前触发一次；
- 每收到一个原始 chunk 触发一次；
- callback 异常不得吞掉模型异常，也不得改变非子代理调用方；实现应让 gateway callback 本身无异常或在子代理封装处隔离。

只有 `src/voidx/agent/adapters/langgraph/runtime/subagent.py` 传入该 callback；主 agent 和 compaction 调用保持默认 `None`。

每次模型 attempt 使用唯一内部 activity ID；start、chunk touch 和 finish 必须使用同一个 ID。retry attempt 使用新的 ID。

扩展 `src/voidx/agent/ports/subagent.py`：

```python
def start_model_activity(self, run_id: str, *, activity_id: str) -> None: ...
def touch_model_activity(self, run_id: str, *, activity_id: str) -> None: ...
def finish_model_activity(
    self,
    run_id: str,
    *,
    activity_id: str,
    succeeded: bool,
) -> None: ...
```

`src/voidx/agent/adapters/langgraph/runtime/subagent.py` 的 `stream_child_llm()` 为每个 attempt 生成 activity ID：

1. 调用 `start_model_activity()`；
2. 把 `touch_model_activity()` 封装成 `stream_llm(on_activity=...)` callback；
3. 在 `finally` 中调用 `finish_model_activity()`；仅正常返回时 `succeeded=True`。

模型 retry sleep 不产生 activity；新的 retry attempt 会创建新的 thinking activity。未知或重复 activity ID 的 touch/finish 不修改累计状态。

### 8.4 工具活动接入

扩展 `src/voidx/agent/ports/subagent.py`：

```python
def start_tool_activity(
    self,
    run_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    args: dict,
    workspace: str,
) -> None: ...
```

`src/voidx/agent/adapters/langgraph/runtime/subagent.py` 传入结构化 `args` 和 workspace，仅供分类、唯一文件计数和内部去重使用。

provider 没有给 `tool_call_id` 时，在同一次 `run_one()` 内生成稳定 fallback activity ID，并把同一 ID 用于 start/finish；空字符串不得作为幂等键。

## 9. 进度与活动渲染

在 `src/voidx/agent/application/subagent_status.py` 提供纯函数：

```python
def public_child_run_snapshot(run: AgentRun) -> dict: ...
def render_child_progress(progress: AgentProgress) -> str: ...
def render_child_activity(run: AgentRun, *, sampled_at: float) -> list[str]: ...
def activity_recommendation(
    run: AgentRun,
    *,
    wait_started_at: float,
) -> Literal["wait", "cancel"]: ...
```

### 9.1 Progress 固定顺序

1. `files_read`
2. `files_edited`
3. `commands_run`
4. `searches`
5. `other_actions`

示例：

```text
read 1 file
read 7 files · edited 3 files · ran 4 commands · searched 6 times
ran 1 command · 2 other actions
```

计数全为零时返回空字符串。

### 9.2 Current / Recent

类别文案固定：

| category | 文案 |
|---|---|
| `thinking` | `thinking` |
| `reading` | `reading` |
| `editing` | `editing` |
| `running_command` | `running command` |
| `searching` | `searching` |
| `other` | `other` |

不得渲染具体 `tool_name`。

## 10. 等待结果状态精简

`src/voidx/agent/adapters/tools/subagent_control.py`：

1. 删除 `_success_item()` 顶层 `terminal`。
2. timeout outcome 使用 `timed_out`。
3. timeout 文本简化为：

```text
Wait timed out after 256s.
```

4. 追加抽象 `Status`、`Progress`、`Current`、`Recent`。
5. 生成 hint 时使用同一次 `sampled_at`，避免文本 age 与建议跨阈值不一致。
6. `_WAIT_TIMEOUT` 继续是默认等待窗口；测试可 monkeypatch 为更短时间，recommendation 仍以实际 `wait_started_at` 为边界。

`run` metadata 使用公开快照，不包含具体工具字段。

## 11. 兼容性与迁移

1. 旧 `wait_outcome="timed_out_still_running"` 不双写；内部代码、测试和 `scripts/repro_wait_blocking.py` 统一迁移到 `timed_out`。
2. 顶层 `terminal` 删除；调用方通过：

```python
status in {"completed", "failed", "cancelled"}
```

判断终态。
3. `AgentRun` 新字段有可选默认值，现有独立构造点保持兼容；网关创建真实 root/sub run 时必须显式初始化抽象 activity 和 `last_activity_at`。
4. 现有内部 `active_tools/last_tool` 行为可保留，避免无关重构；主 agent 的文本和 metadata 不再暴露它们。
5. runtime Current Task State 的 child status 也改用抽象 `Current/Recent`，不显示具体工具名。
6. 正常完成结果文本保持不变；抽象进度主要用于 running timeout 和运行时 child status。

## 12. 文件职责

| 路径 | 责任 |
|---|---|
| `src/voidx/agent/domain/subagent.py` | 抽象 activity/progress 模型、`last_activity_at`、timeout outcome |
| `src/voidx/agent/ports/subagent.py` | 模型/工具活动记录接口 |
| `src/voidx/agent/adapters/subagent/inprocess_gateway.py` | 活动状态机、幂等累计、文件去重、公开快照所需状态 |
| `src/voidx/agent/adapters/langgraph/runtime/streaming.py` | 可选 stream activity callback |
| `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | 模型与工具活动接入 |
| `src/voidx/agent/application/subagent_status.py` | 抽象进度、状态、activity age 和建议纯函数 |
| `src/voidx/agent/adapters/tools/subagent_control.py` | 公开快照、timeout 文本、动态 next step hint |
| `scripts/repro_wait_blocking.py` | timeout outcome 与等待期间无活动 hint 回归 |
| `src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py` | 累计、活动状态、幂等、文件去重 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_gateway_result.py` | 真实模型/工具 activity 接入 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py` | 可选 stream callback |
| `src/tests/test_application/test_subagent_status.py` | 抽象渲染和 wait/cancel 建议边界 |
| `src/tests/test_tooling/test_agent_control.py` | 主 agent 可见文本、metadata 和 hint |
| `src/tests/test_tooling/test_interactive_tools.py` | spawn/wait 集成与 outcome 迁移 |
| `src/tests/test_application/test_runtime_context_builder.py` | Current Task State 不暴露具体工具名 |

## 13. TDD 实施顺序

### Task 1：抽象领域模型与纯函数

- [ ] 在 `src/tests/test_application/test_subagent_status.py` 添加：
  - progress 固定顺序和单复数；
  - 具体工具名不出现在 Current/Recent；
  - `last_activity_at >= wait_started_at -> wait`；
  - `last_activity_at < wait_started_at` 或时间缺失 `-> cancel`。
- [ ] 运行并确认 RED：

```bash
./test.py --backend -- src/tests/test_application/test_subagent_status.py
```

- [ ] 修改 `src/voidx/agent/domain/subagent.py` 与 `src/voidx/agent/application/subagent_status.py`。
- [ ] 运行同一命令确认 GREEN。

### Task 2：网关活动状态与抽象累计

- [ ] 在 `src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py` 添加：
  - activity start/touch/finish 更新 `last_activity_at`；
  - 并发时只选一个最新 current category；
  - 重复 start/touch/finish 幂等；
  - read/edit 唯一文件计数；
  - command/search/other 抽象次数；
  - 具体工具字段不进入公开快照；
  - `wait_outcome="timed_out"`。
- [ ] 运行并确认 RED：

```bash
./test.py --backend -- src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py
```

- [ ] 修改 `src/voidx/agent/ports/subagent.py` 与 `src/voidx/agent/adapters/subagent/inprocess_gateway.py`。
- [ ] 运行同一命令确认 GREEN。

### Task 3：模型与工具活动接入

- [ ] 在 `src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py` 先写 callback RED 测试。
- [ ] 在 `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_gateway_result.py` 先写模型 thinking、stream chunk 和工具抽象状态 RED 测试。
- [ ] 运行：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_subagent_gateway_result.py
```

- [ ] 修改 `src/voidx/agent/adapters/langgraph/runtime/streaming.py` 与 `src/voidx/agent/adapters/langgraph/runtime/subagent.py`。
- [ ] 运行同一命令确认 GREEN。

### Task 4：主 agent 可见契约

- [ ] 在 `src/tests/test_tooling/test_agent_control.py` 添加或更新：
  - 顶层无 `terminal`；
  - outcome 为 `timed_out`；
  - run metadata 无具体工具字段；
  - 输出只有抽象 Progress/Current/Recent；
  - 本次等待期间有活动时 hint 建议 wait；
  - 本次等待期间无活动时 hint 建议 cancel；
  - batch hint 按 agent 分行。
- [ ] 在 `src/tests/test_application/test_runtime_context_builder.py` 添加具体工具名不进入 Current Task State 的断言。
- [ ] 运行并确认 RED：

```bash
./test.py --backend -- \
  src/tests/test_tooling/test_agent_control.py \
  src/tests/test_application/test_runtime_context_builder.py
```

- [ ] 修改 `src/voidx/agent/adapters/tools/subagent_control.py` 与相关 renderer。
- [ ] 运行同一命令确认 GREEN。

### Task 5：集成与完整验证

- [ ] 更新 `src/tests/test_tooling/test_interactive_tools.py` 和所有旧 outcome 断言。
- [ ] 更新 `scripts/repro_wait_blocking.py`，覆盖等待期间无活动的 cancel 建议。
- [ ] 运行相关测试：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/subagent \
  src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_subagent_gateway_result.py \
  src/tests/test_application/test_subagent_status.py \
  src/tests/test_application/test_runtime_context_builder.py \
  src/tests/test_tooling/test_agent_control.py \
  src/tests/test_tooling/test_interactive_tools.py
```

- [ ] 运行复现脚本：

```bash
./python.py scripts/repro_wait_blocking.py
```

- [ ] 运行完整后端：

```bash
./test.py --backend
```

## 14. 禁止项与不变量

实现不得：

1. 在主 agent 可见文本或 metadata 中保留具体工具名、tool ID、工具参数、命令或文件路径；
2. 暴露 `tool_counts` 或总工具调用数；
3. 从 Bash 文本拆分命令或从工具输出解析文件路径；
4. 用定时 heartbeat 刷新 `last_activity_at`；
5. 因 asyncio task 仍存在就判断任务活跃；
6. 因 wait timeout 自动取消子代理；
7. 把 timeout 返回标记为 ToolResult error；
8. 在 `wait` 时扫描持久化日志重建进度；
9. 修改 compaction 或正常结果契约。

必须保持：

- 默认 wait 窗口为 256 秒，建议以每次 wait 的实际开始时间为活动边界；
- timeout 返回时 lifecycle status 仍为 running；
- 模型 chunk 和工具边界都刷新真实可观测活动时间；
- 文件集合只在直接文件工具成功后合并；
- 重复活动 ID 不重复累计；
- Current 始终只有一个抽象类别；
- 无进度或无 recent 时不渲染空行；
- 文案不把“无观测活动”断言成“任务已卡死”。

## 15. 验收标准

1. 主 agent 的 timeout 输出只包含抽象进度：

```text
Progress: read 7 files · edited 3 files · ran 4 commands · searched 6 times
Current: searching · activity 12s ago
Recent: editing · succeeded 21s ago
```

2. 输出和 metadata 中没有 `search`、`replace`、`bash` 等具体工具名字段；抽象文案 `searching`、`editing` 和 `running command` 合法。
3. metadata 没有顶层 `terminal`，timeout outcome 为 `timed_out`。
4. 本次 wait 开始后观察到活动时，hint 建议 wait。
5. 本次 wait 开始后没有观察到活动时，hint 建议 cancel，并明确写 `no activity was observed during the 256s wait`。
6. LLM attempt 开始和每个 stream chunk 都刷新 `last_activity_at`；无人工 heartbeat。
7. 同一文件重复 read/edit 不重复增加文件数。
8. 一次 Bash 调用只增加一次 command；不解析内部 shell 命令。
9. runtime Current Task State 不再显示具体 active/last tool 名称。
10. 聚焦测试、复现脚本和完整后端测试全部通过。
