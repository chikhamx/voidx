# Agent 工具精简与子 Agent Workflow 隔离设计

## 状态

已实现，测试覆盖完成。

## 目标

精简主 agent 委派子 agent 的调用契约，隐藏可推导的内部字段，拆分任务启动与运行控制，并让子 agent 只接收其当前工作类型所需的 workflow prompt。

## 背景与问题

当前 `agent` 工具位于 `src/voidx/tools/agent.py`，同时承担三类职责：

- 启动子 agent：`spawn`；
- 等待子 agent：`wait`；
- 取消子 agent：`cancel`。

当前输入还暴露了多个可推导或重复字段：

- `name`：当前唯一子 agent identity 为 `voidx`；
- `success_criteria`：只是任务 prompt 的一部分，与 `task` 重复；
- `result_preset`：已经可以由 `mode` 自动推导；
- `target`：名称不能清晰表达它是任务范围；
- 数值 `timeout`：把底层等待实现细节直接暴露给 LLM。

当前 `run_subagent()` 还传入完整的 `WORKFLOW_RUNTIME`。该 prompt 来自整个 workflow DAG，约 9,830 字符，包含主 agent 专属的 `brainstorm`、`design`、`plan` 等节点；子 agent 实际只需要当前委派 route 的局部节点。

## 设计原则

1. LLM 只填写无法由系统可靠推导的任务信息。
2. 任务目标、任务细节和任务范围分开表达。
3. 任务启动与已有 run 的控制分开表达。
4. 子 agent mode 表示稳定的工作能力，不表示全部 workflow 节点。
5. 主 agent 专属的用户审批流程不开放给子 agent。
6. LLM-facing schema 使用语义化枚举；底层 gateway 继续使用数值 timeout。
7. 保持 gateway 的父子路由、安全和生命周期语义不变。

## 最终工具面

### `agent`

职责：启动一个隔离的子 agent，并立即返回 `run_id`。

描述：

```text
Start one isolated child agent for an independent task and return its run_id. The child does not inherit the caller's conversation history.
```

参数：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `mode` | `review \| debug \| implement` | 是 | 子 agent 的工作类型 |
| `goal` | string | 是 | 一句话任务目标；直接构造 `GoalSpec.desc` |
| `detail` | string | 是 | 完整执行说明，包括背景、约束、步骤和验收标准 |
| `scope` | string \\| null | 否 | 文件、模块、目录、行为或问题范围 |

示例：

```json
{
  "mode": "implement",
  "goal": "精简 agent 工具参数",
  "detail": "删除 name 和 result_preset，将 success_criteria 合并到任务说明中，更新 schema 和相关测试，并运行定向测试。",
  "scope": "src/voidx/tools/agent.py"
}
```

`scope` 不应强制要求；当范围已包含在 `detail` 或 `goal` 中时可以省略。

### `agent_control`

职责：等待或取消已经创建的子 agent run。

描述：

```text
Wait for or cancel an existing child-agent run.
```

参数：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `action` | `wait \| cancel` | 是 | 控制操作 |
| `run_id` | string | 是 | 子 agent run ID |
| `wait` | `brief \| extended \| until_complete` | `wait` 时建议填写 | 等待策略；`cancel` 时忽略 |

示例：

```json
{
  "action": "wait",
  "run_id": "sub:...",
  "wait": "extended"
}
```

```json
{
  "action": "cancel",
  "run_id": "sub:..."
}
```

## 字段语义与内部映射

### `goal`

`goal` 是稳定、简短、可展示的一句话目标，应描述最终要达成的结果，而不是执行过程。它直接构造：

```python
GoalSpec(desc=inp.goal.strip())
```

约束：

- 非空；
- 一句话；
- 建议以动词开头；
- 不承载长背景、完整步骤或测试列表。

示例：

```text
定位 API 请求偶发超时的根因
审查 session 持久化实现
实现消息接收等待策略
```

### `detail`

`detail` 是子 agent 执行所需的完整任务 brief。它替代旧的 `task` + `success_criteria` 组合，应包含：

- 必要背景；
- 具体要求；
- 限制条件；
- 预期输出；
- 完成标准；
- 需要运行的测试或验证命令。

子 agent 接收的任务描述建议渲染为：

```text
Goal: <goal>

Scope: <scope>        # scope 存在时

Details:
<detail>
```

### `scope`

`scope` 替代旧字段 `target`，明确表示任务范围，不表示运行目标 ID。内部所有任务描述、goal 生成和相关 metadata 应统一使用 `scope`，不保留 `target` 与 `scope` 双重术语。

描述：

```text
Optional file, module, directory, behavior, or issue scope for the delegated task.
```

### `wait`

`wait` 替代 LLM-facing 的数值 `timeout`，使用语义化字符串：

| 值 | 含义 | gateway timeout |
|---|---|---:|
| `brief` | 等一会儿后返回当前状态 | `5.0` 秒 |
| `extended` | 等长一点后返回当前状态 | `30.0` 秒 |
| `until_complete` | 一直等到子 agent 进入终态 | `0.0` |

映射应集中在工具层，例如：

```python
_WAIT_TIMEOUTS = {
    "brief": 5.0,
    "extended": 30.0,
    "until_complete": 0.0,
}
```

`AgentGateway.wait()` 保持现有数值 `timeout` API，不把语义枚举传入基础设施层。`brief` 和 `extended` 的秒数是工具协议实现常量，后续如需调整只修改单一映射表。

`wait` 的默认值建议为 `until_complete`，保证省略等待策略时仍然保持当前 `timeout=0` 的无限等待语义。

## 子 agent mode

公开 mode 只保留三个：

```text
review
debug
implement
```

### `review`

覆盖旧的 `inspect` 和 `review`。

职责：

- 调查当前行为；
- 收集文件、代码和测试证据；
- 审查正确性、完整性、风格和风险；
- 默认只读，不修改文件。

route：

```text
review -> review
```

### `debug`

职责：

- 读取完整错误并复现；
- 对比可工作示例；
- 定位根因；
- 输出证据、复现信息和修复方向。

`debug` 子 agent 不自动进入实现流程；需要修改时由主 agent 再启动 `implement`，或由明确的 implement 任务承接。

route：

```text
debug -> debug
```

### `implement`

覆盖旧的 `implement` 和 `feedback`。

职责：

- 修改代码；
- 编写或更新测试；
- 落实 review feedback；
- 验证实现结果。

review feedback 直接写入 `detail`，不再使用独立的 `feedback` mode。

route：

```text
tdd -> verify
```

## 不开放给子 agent 的 workflow

以下 workflow 属于主 agent 的用户交互或调度流程，不作为子 agent mode：

- `brainstorm`：需要澄清和用户批准；子 agent 没有可靠的用户交互能力；
- `design`：文档质量 gate 和用户可读性验证属于主 agent 流程；
- `plan`：实施计划审批属于主 agent 流程；
- `feedback`：已并入 `implement` 的任务语义。

主 agent 的推荐流程仍然可以是：

```text
brainstorm -> design -> plan -> tdd -> verify
```

子 agent 只负责被委派的局部工作：

```text
review

debug

tdd -> verify
```

## 局部 workflow prompt

### 当前问题

`run_subagent()` 当前把完整 `WORKFLOW_RUNTIME` 传给 `RuntimeContextBuilder`。完整 prompt 来自 `WorkflowService().context()`，包含整个 DAG 的节点定义和主 agent 专属节点。

### 目标行为

子 agent 只接收：

1. 通用 workflow 运行规则；
2. 当前 mode 对应 route 的 `join`、`leave` 信息；
3. route 涉及的 workflow node 定义。

不接收无关节点和全局 DAG 边。

### 局部 prompt 映射

| mode | active nodes | join | leave |
|---|---|---|---|
| `review` | `review` | `review` | `review` |
| `debug` | `debug` | `debug` | `debug` |
| `implement` | `tdd`, `verify` | `tdd` | `verify` |

推荐渲染结构：

```text
## Workflow Runtime

- Current Task State is the sole source of active workflow nodes.
- Only the active workflow nodes below are normative.

### Active route

- Join: tdd
- Leave: verify

### tdd

<rendered tdd instruction>

### verify

<rendered verify instruction>
```

局部 prompt 应由 workflow service 根据节点名称生成，避免复制 workflow 节点正文。主 agent 继续使用完整 workflow runtime；只对子 agent 使用局部版本。

### 子 agent persona

建议让 mode 与子 agent persona 对齐：

| mode | persona |
|---|---|
| `review` | `review` |
| `debug` | `explore` |
| `implement` | `implement` |

当前 `run_subagent()` 默认使用 `explore`，实现时应改为根据 mode 或 normalized delegation 显式传递 persona，避免 implement 子 agent 获得只读探索角色提示。

## 结果契约

`result_preset` 不再是公开输入字段。结果契约由 mode 内部推导：

| mode | result schema |
|---|---|
| `review` | `review_result` |
| `debug` | `debug_result` |
| `implement` | `implementation_result` |

`AgentResultContract` 和结构化重试机制可以继续保留，但调用方不再选择 preset，也不再产生 mode/preset 不一致组合。

建议保留现有结果字段：

- review：`verdict`, `findings`, `risks`, `next_actions`；
- debug：`root_cause`, `evidence`, `reproduction`, `fix_direction`, `open_questions`；
- implement：`status`, `files_changed`, `tests_run`, `risks`, `followups`。

## Agent run 控制与返回结果

`agent` spawn 成功后返回：

- `run_id`；
- 当前 status；
- 简短的 run metadata；
- 下一步提示使用 `agent_control`。

不再返回可由 `run` 重复推导的大段 intent、goal、workflow route，除非 UI 或审计确实需要。`goal` 和 `scope` 可以保存在 run description/metadata 中，但应避免在 output、summary 和 metadata 中重复三次。

`agent_control(wait)` 的行为保持不变：

- `brief`/`extended` 超时返回当前 run 状态，不取消 run；
- `until_complete` 等待到 `completed`、`failed` 或 `cancelled`；
- 可以重复等待；
- `cancel` 仅取消合法父子路由中的目标 run。

底层 `AgentGateway` 的父子关系、session 校验、消息队列和生命周期消息不改变。

## Message 工具边界

本设计首先改造 agent 工具。`message` 的 gateway 层仍使用数值 `timeout`，避免把工具等待策略和基础设施 API 混在一起。

后续如需统一 LLM-facing 语义，可将 `message.receive` 的数值 `timeout` 改成独立的等待枚举，例如 `brief`、`extended`、`until_message`；但不在本次 agent 工具改造中同时改变 message 协议，以控制迁移范围。

## 迁移规则

删除或重命名以下公开字段：

| 旧字段 | 新处理 |
|---|---|
| `name` | 删除；内部固定使用唯一 child identity `voidx` |
| `mode=inspect` | 映射到 `review` |
| `mode=feedback` | 映射到 `implement` |
| `mode=plan` | 不再允许作为子 agent mode；由主 agent 处理 |
| `target` | 重命名为 `scope` |
| `task` | 拆为 `goal` + `detail` |
| `success_criteria` | 合并进 `detail` |
| `result_preset` | 删除；按 mode 内部推导 |
| `action=wait` | 移到 `agent_control.action=wait` |
| `action=cancel` | 移到 `agent_control.action=cancel` |
| `target_run_id` | 重命名为 `agent_control.run_id` |
| 数值 `timeout` | 改为 `agent_control.wait` 语义枚举 |

是否接受旧字段兼容应由实现阶段决定。推荐 LLM-facing schema 直接切换到新契约；内部测试和迁移 helper 可以在短期内接受旧输入，但不得继续把旧字段暴露在新 schema 中。

## 受影响文件

预计修改：

- `src/voidx/tools/agent.py`：重建 spawn 输入、mode route、goal/detail/scope 归一化和控制逻辑；
- 新增 `src/voidx/tools/agent_control.py`：实现 wait/cancel 工具；
- `src/voidx/agent/infrastructure/langgraph/runtime/wiring.py`：注册新工具并更新 agent surface；
- `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py`：注入局部 workflow prompt、根据 mode 设置 persona，并继续屏蔽嵌套 delegation；
- `src/voidx/agent/application/prompts.py` 或新增 workflow prompt helper：提供通用规则和局部节点渲染；
- `src/voidx/agent/application/agents.py`：更新子 agent 描述，删除对可选 child identity 的公开暗示；
- 相关 agent/tool/workflow 测试：更新 schema、调用、mode、结果和 prompt 断言。

不预计修改：

- `src/voidx/agent/gateway/gateway.py`：保留数值 timeout 的基础设施 API；
- `src/voidx/agent/gateway/models.py`：run/message 模型不因工具字段重命名而改变。

## 测试与验收标准

### Schema

- `agent` schema 只包含 `mode`、`goal`、`detail`、`scope`；
- mode 枚举严格为 `review`、`debug`、`implement`；
- 不暴露 `name`、`success_criteria`、`result_preset`、`action`、`target_run_id`、`timeout`；
- `agent_control` schema 只包含 `action`、`run_id`、`wait`；
- `wait` 枚举严格为 `brief`、`extended`、`until_complete`。

### 行为

- `goal` 正确构造 `GoalSpec.desc`；
- `scope` 正确进入子 agent description 和 metadata；
- `detail` 被完整传递给子 agent；
- `review`、`debug`、`implement` 生成正确 route 和结果契约；
- `agent_control` 的三种等待策略映射到正确的 gateway timeout；
- `cancel` 不读取或要求 `wait`；
- 父子路由和跨 session 拒绝行为保持不变。

### Prompt

- review 子 agent 只看到 `review` node；
- debug 子 agent 只看到 `debug` node；
- implement 子 agent 只看到 `tdd` 和 `verify` nodes；
- 子 agent prompt 不包含 `brainstorm`、`design`、`plan` 或无关 DAG 边；
- implement 子 agent 使用 implement persona；
- 主 agent 仍使用完整 workflow prompt。

### 推荐验证命令

```bash
./test.py --backend -- src/tests/test_tools/test_agent.py -v
./test.py --backend -- src/tests/test_tools/test_agent_control.py -v
./test.py --backend -- src/tests/test_infrastructure/runtime/test_graph_setup_prompts.py -v
./test.py --backend -- src/tests/test_infrastructure/runtime/test_subagent_gateway_result.py -v
./test.py --backend -- src/tests/test_tools/test_interactive_tools.py -v
./test.py --backend
```

如果实现阶段沿用现有测试文件而不新建 `test_agent.py` 或 `test_agent_control.py`，应将命令替换为实际文件路径；验收要求不变。实现完成后，必须新增或更新针对新 agent schema、agent_control、局部 workflow prompt 和等待映射的定向测试，再执行完整 backend suite。

## Token 影响估算

当前 `agent` 定义约 2,234 字符，粗略约 550～640 token；完整 workflow runtime 约 9,830 字符，粗略约 2,400～2,800 token。

预期：

- spawn-only `agent` schema 约 220～300 token；
- `agent_control` schema 约 140～200 token；
- 子 agent review workflow prompt 约 300～400 token；
- 子 agent debug workflow prompt 约 400～500 token；
- 子 agent implement workflow prompt 约 600～800 token。

相较当前完整 workflow prompt，局部渲染预计节省约 65%～85%，具体取决于 mode。工具拆分增加少量静态 tool definition token，但降低条件参数推理和错误重试成本。

## 非目标

本次不做：

- 修改 `AgentGateway` 的内部数值 timeout API；
- 改变 agent run 的父子权限模型；
- 允许子 agent 与用户直接交互；
- 允许子 agent 嵌套调用 `agent`；
- 将 `brainstorm`、`design`、`plan` 开放为子 agent mode；
- 同时重构 `message` 的 LLM-facing timeout 字段；
- 引入动态工具 surface；
- 改变 workflow DAG 本身的主 agent 转移规则。
