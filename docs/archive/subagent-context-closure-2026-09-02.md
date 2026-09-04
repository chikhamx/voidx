# 子 Agent 上下文与父子结果闭环修复 — 技术规格

> **Status: Done** — Archived on 2026-09-04.
> **Date: 2026-09-02**
> **Audience: Human + LLM**

## TL;DR

从 LLM 视角，子 Agent 当前只有“局部闭环”：它能读取任务、调用工具并产出终止文本，但以下链路不完整或不一致：

1. 子 Agent registry 通过浅拷贝共享可变插件实例，重新绑定 runtime 可能污染父 Agent。
2. review 子 Agent 以异步 `agent -> spawn -> agent_control(wait)` 返回结果，而自动推进只检查同步 `agent` 结果，且结果格式和 mode metadata 均不一致。
3. debug 的 `explore`/只读 persona 与实际可见的写入、shell 工具冲突。
4. 子 Agent 可见 `agent_control`，但没有可控制的子 run，形成不可用工具。
5. `message(question|answer|message)` 进入父 inbox 后，没有稳定的父 Agent LLM 消费与唤醒闭环；只有 `message(result)` 具有终止语义。
6. 子 Agent 默认没有获得父层项目 instructions、profile sections 和 summary，导致它看到的规则与实际项目约束不完整。
7. 父 Agent 在新的一般性/meta 请求下可能保留旧的 active workflow，造成“当前请求”与“旧 workflow gate”并存的语义冲突。

本规格修复这些一致性问题，同时保留既有单节点 workflow 约束、gateway 生命周期、权限快照和父 Agent 的 workflow 所有权。

## 1. 背景与问题定义

### 1.1 LLM 需要的完整闭环

子 Agent 的可执行闭环必须满足：

```text
父 Agent delegation
  -> 明确的 goal / scope / detail / mode / result contract
  -> 子 Agent 看到与 mode 一致的 system context 和 tool surface
  -> 子 Agent 读取/执行工具
  -> 工具结果更新子 Agent 本地状态和下一轮上下文
  -> 子 Agent 以结构化、可识别的终止结果结束
  -> gateway 固化 run status/result
  -> 父 Agent wait 或异步唤醒取得同一份权威结果
  -> 父 Agent 消费结果并决定 feedback / verify / debug / done
```

任何一段只存在于自然语言、UI 摘要或不可消费的 inbox 中，都不算完成闭环。

### 1.2 当前实现中的已确认差距

#### A. registry 容器隔离但实例未隔离

`ToolRegistry.filtered_copy()` 复制 `_tools` 和 `_instances` 字典，但保留相同 plugin 对象：

- `src/voidx/tooling/application/registry.py:126-131`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py:168-200`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py:467-496`

子 Agent 会调用 `bind_agent_tool_runtime()` 和 `bind_scoped_tools()` 改写 plugin 内部 runtime、授权器、文件状态或 invoker。当前最小复现显示，父 registry 中共享的 agent-owned plugin 可被改成 `child-run`。

**影响**：并行父/子执行时可能出现错误 run_id、错误 inbox 目标、错误权限快照或文件状态串线。此问题优先级为 P0。

#### B. review 自动推进不能覆盖真实异步 delegation

当前自动推进入口：

- `src/voidx/agent/application/automation/workflow/auto_advance.py:39-88`
- `_check_review_result()` 要求 `tool_name == "agent"`、`metadata["agent"] == "review"`，并匹配 `verdict: FAIL|NEEDS_CHANGE`。

当前 delegation 入口：

- `src/voidx/agent/adapters/tools/subagent.py:191-232`
- `agent` 工具首先返回 `[running]` 和 `run_id`，真实结果在 `agent_control(wait)` 的 ToolResult 中出现。
- metadata 使用 public agent identity `voidx`，不是输入 mode `review`。
- review result contract 当前为 `verdict=PASS|FAIL|NEEDS_CHANGE, ...`，与 regex 要求的冒号格式不一致。

因此真实路径：

```text
agent(review) -> running
agent_control(wait) -> completed result
```

不会稳定触发 `review_has_issues -> feedback`。最小复现已经证明以下三种输入均无法产生该事件：`verdict=FAIL + agent=review`、`verdict: FAIL + agent=voidx`、`agent_control(wait)` 返回 review 结果。

**影响**：父 Agent 可能看到了 review FAIL，却没有进入预期的 feedback 分支，闭环断裂。此问题优先级为 P0。

#### C. mode persona 与工具能力不一致

`debug` 子 Agent 根据 workflow 节点获得 `explore` persona，并看到：

```text
Do not write or edit files.
```

但 child tool surface 只阻止 `agent`、`clarify`、`checkpoint`、`workflow`，仍可能包含 `write`、`replace`、`manage`、`bash` 等工具：

- `src/voidx/agent/application/prompts.py:398-405`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py:94-105`
- `src/voidx/agent/adapters/langgraph/runtime/tool_surface.py:24-26,102-104`

**影响**：模型同时收到“只读”指令和“可写” affordance；即使权限层最终拒绝，也会浪费推理轮次，并可能在权限配置允许时误修改工作区。此问题优先级为 P0/P1，最终实现必须把只读约束落到 tool surface 或硬授权层，而不是只改 prompt。

#### D. 子 Agent 可见无效的 `agent_control`

`agent_control` 被注册在父 registry，当前 child blocked 集合没有包含它；子 Agent 因而可能看到 `agent_control`。但 gateway 的控制路由只允许 root 控制自己的 child：

- `src/voidx/agent/adapters/langgraph/runtime/subagent.py:95`
- `src/voidx/agent/adapters/langgraph/runtime/tool_surface.py:24-26`
- `src/voidx/agent/domain/subagent.py:114-118`

子 Agent 没有可控制的 child run，调用该工具只能得到 route error。

**影响**：模型看到不可用工具，可能反复尝试 wait/cancel。此问题优先级为 P1。

#### E. 普通父子消息没有消费闭环

`MessageTool` 支持 `message`、`question`、`answer`、`result`：

- `src/voidx/agent/adapters/tools/subagent_message.py:13-27,55-103`

普通消息进入父 run inbox；父层主要通过 `agent_control(wait)` 读取 child run 状态和结果，当前没有统一的“父 inbox -> 父 LLM 上下文 -> 父回答 -> 子 Agent resume”路径。`result` 会固化 child terminal result，普通消息不会。

**影响**：子 Agent 可以发出问题，但父 Agent 可能永远看不到或不能回复；模型会误以为 message tool 是双向交互工具，实际只有终止 result 稳定可用。此问题优先级为 P1。

#### F. 项目 instructions 与子 Agent 上下文不一致

`run_subagent()` 构建子上下文时传入 base prompt、persona、workflow runtime 和 task state，但没有传递父层 `instructions`、`profile_sections` 或 summary：

- `src/voidx/agent/adapters/langgraph/runtime/subagent.py:376-399`
- `src/voidx/agent/application/runtime_context.py:172-225`

父 Agent 可通过 `detail` 手工复制部分规则，但这不具备稳定性，也不能保证子目录 `AGENTS.md`、测试入口和项目边界完整传递。

**影响**：子 Agent 的模型认知与其实际执行环境不一致。此问题优先级为 P1；本规格要求定义最小、可审计的 context handoff，不允许隐式复制完整父 transcript。

#### G. 新 general/meta 请求仍可能携带旧 active workflow

`TaskState.update_after_turn()` 在已有 active workflow 时，对 `general` intent 保留 active workflow：

- `src/voidx/agent/domain/task/state.py:75-80`

因此父 Agent 可能同时看到：

```text
Intent: general
Active workflow nodes: feedback
Current persona: implement
```

**影响**：模型难以判断当前请求是“审查 workflow 设计”还是“继续执行 feedback”；旧 gate 可能污染新请求。此问题优先级为 P1，目标不是无条件删除 workflow，而是显式区分“当前请求是否继承旧任务”。

## 2. 目标与非目标

### 2.1 目标

1. 父/子 registry 在容器、plugin 实例、runtime binding、scoped authorization 和 file state 层面都隔离。
2. review 结果无论通过 `agent` 同步结果、`agent_control(wait)` 终态结果还是 gateway terminal snapshot，都能被父层统一消费，并可靠驱动既有 auto-advance。
3. 子 Agent 的 LLM-visible tool surface 与 mode/persona 的能力边界一致：
   - review：可读、分析、汇报，不可自行进入父 workflow 或继续 delegation；
   - debug：只读调查和验证，不可写文件或执行具有写副作用的工具；
   - implement：可在权限允许范围内修改和测试，但不拥有父 workflow 编排权。
4. 子 Agent 不再看到没有合法控制对象的 `agent_control`，除非未来定义并实现嵌套子 Agent 控制协议。
5. `message(result)` 继续作为终止结果通道；普通 `message/question/answer` 要么建立完整父子交互闭环，要么从子 Agent surface 移除，不能保留半可用 affordance。
6. 子 Agent 获得完成任务所需的、经过明确裁剪的 project instructions/profile context，不继承父完整对话或父私有状态。
7. 新的 general/meta 请求不会被旧 workflow gate 误导；若用户明确要求继续旧任务，仍可恢复旧 workflow。
8. 保留既有单节点子 Agent workflow：标准 route 仍为 `review -> review`、`debug -> debug`、`implement -> tdd`，父 Agent 独占跨节点路由和最终 fresh verification。

### 2.2 非目标

1. 不修改 `DEFAULT_WORKFLOW_DAG` 的节点、边和 terminal 语义。
2. 不恢复子 Agent 的 `workflow` 工具；子 Agent 仍不能调用 `workflow`、`agent`、`clarify`、`checkpoint`。
3. 不让 implement 子 Agent 的测试替代父 Agent 的 fresh verification。
4. 不把父 Agent 的完整 transcript、其他 child transcript、权限内部细节或所有 workflow run history 注入子 Agent。
5. 不把普通消息协议扩展成实时 progress 流；progress 仍不属于 transport message type。
6. 不重做 `AgentInput` 的公开 mode enum，也不修改 `AgentResultContract` 的既有 preset 字段语义，除非结构化结果协议单独批准兼容迁移。
7. 不修改主 Agent `workflow` 工具、route-terminal helper 或主 Agent 默认 `show_workflow_transitions=True` 行为。
8. 不在本规格中实现跨 session、跨 sibling 的消息路由。
9. 不以 UI 展示层文本替代 runtime 的结构化状态和结果契约。

## 3. 设计原则与不变量

### 3.1 单一权威来源

- 文件内容、命令输出、测试结果和权限状态必须由工具/执行器提供，模型不可从父摘要猜测。
- child run 的 `AgentRun.status` 和 `AgentRun.result` 是等待结果的权威来源。
- workflow 状态只能由父 Agent workflow runtime 或明确的父层 state update 改变；子 Agent 不直接拥有父 workflow 生命周期。
- review verdict 的机器路由信号必须来自结构化 metadata/report；自然语言 regex 只能作为明确标注的 legacy fallback。

### 3.2 LLM-visible 一致性

对每个子 Agent turn，以下集合必须一致：

```text
visible_tools = actually_executable_tools
visible_persona_constraints = enforced_capabilities
visible_workflow_nodes = allowed_workflow_reference
visible_result_contract = result_parser_accepts
```

禁止模型看到一个工具却无法合法执行，也禁止模型看到“只读/不可交互”规则却拥有相反能力。

### 3.3 父子隔离

- child registry 的每一个可变 plugin instance 必须与 parent instance 不同，除非该对象经过证明为 immutable、无 runtime binding、无内部可变状态的纯值对象。
- child 的 runtime、AuthorizationRuntime、FileStateStore、ProcessSandbox、ToolInvoker 和 message target 必须指向 child scope。
- 绑定 child runtime 不得改变 parent registry 中任何 plugin 的行为或 identity。
- permission snapshot 可以从 parent 创建，但 child 执行期间的 revocation reader 必须遵循既有权限快照语义，不能通过共享 plugin 隐式读取 parent mutable runtime。

### 3.4 结果闭环

任何 child terminal path 都必须最终满足：

```text
child result -> AgentRun.result -> parent wait/snapshot -> parent ToolMessage -> parent LLM context
```

如果父层已收到 terminal result，不能因为 inbox 满、重复 lifecycle、preview 截断或 auto-advance 判断失败而丢失权威结果。

## 4. 目标设计

### 4.1 子 Agent context envelope

子 Agent 每轮上下文由以下部分组成，顺序保持稳定：

1. `Base System`：基础身份、沟通、workspace、verification 和 collaboration 规则。
2. `Persona`：当前 mode 对应 persona；persona 中声明的能力必须由 tool surface/authorization 同步执行。
3. `Workflow Runtime`：当前标准单节点或调用方显式 route 的节点定义；不显示父 DAG 出口。
4. `Runtime State`：child workspace、platform、权限 envelope、profile snapshot 摘要。
5. `Current Task State`：当前 goal、intent、route、active child node、todo；不包含父 Agent 私有 workflow state。
6. `Project Instructions`：经过 handoff policy 明确允许的项目规则，来源可追踪。
7. `Task Payload`：`Goal`、`Scope`、`Details` 和 result contract。
8. 语义消息历史：仅当前 child run 的消息、工具结果和必要的 guidance。

#### 4.1.1 Context handoff policy

新增一个显式、可测试的 handoff 结果，推荐形式：

```python
@dataclass(frozen=True)
class ChildContextHandoff:
    instructions: tuple[str, ...] = ()
    profile_sections: tuple[ContextSection, ...] = ()
    summary: str = ""
    source_paths: tuple[str, ...] = ()
```

要求：

- 父层只传递本次 child scope 必需的 project instructions/profile sections；不得传递完整父 prompt 或完整历史。
- `summary` 只能是与 delegated task 相关的压缩事实；不得把父 Agent 的未验证猜测当作事实。
- 若 handoff 无法构造，子 Agent 仍可启动，但 task payload 必须标记缺失 context，模型不得声称已遵守未知项目规则。
- 不改变 `AGENTS.md` 动态发现机制的所有权；若子 Agent 通过 `read` 访问新目录，既有 instruction resolver 仍可按当前规则注入。

具体 handoff API 可由实现者选择，但必须在测试中能独立断言传入了哪些 section、来源和内容。

### 4.2 mode 到 capability 的明确映射

新增单一 source of truth，避免 persona 文案、tool surface 和 authorization 各自维护不同规则。推荐使用 mode/capability policy：

| Mode | Persona | 必须可见 | 必须不可见/不可执行 |
|---|---|---|---|
| `review` | `review` | read-only inspection、search、LSP read、document、web read、message(result)（若保留） | `agent`、`clarify`、`checkpoint`、`workflow`、`agent_control`、write/replace/manage、写副作用 shell |
| `debug` | `explore` | read-only inspection、search、LSP read、document、web read、必要的无写验证命令 | `agent`、`clarify`、`checkpoint`、`workflow`、`agent_control`、write/replace/manage、写副作用 shell |
| `implement` | `implement` | review/debug 的读取能力，加上受权限层保护的 write/replace/manage/bash、todo、message(result) | `agent`、`clarify`、`checkpoint`、`workflow`、`agent_control` |

说明：

- “写副作用 shell”必须由现有 shell authorization/risk classification 判定，不能仅凭命令字符串在 prompt 层猜测。
- 如果当前权限架构无法在 tool definition 阶段区分 shell 子操作，则 debug 至少必须把 `bash/powershell` 从 surface 移除，或者把所有 shell 调用送入不可写的专用 sandbox policy。
- `todo` 是否对 review/debug 可见应由实际需求决定；若保留，必须保证 child-local tracker，不得读写 parent tracker。
- `message` 若仅保留终止结果，应把 schema 收窄为 `message_type="result"`；若保留普通消息，必须实现 §4.5 的交互闭环。

### 4.3 真正隔离 child registry

实现必须提供 child-specific registry 构造路径。可接受方案包括：

1. 为每个 plugin 增加显式 `clone_for_child()`/factory；
2. 在 composition root 保存 plugin factories，每次 child run 重新构建实例；
3. 对明确无状态 plugin 使用 immutable shared instance，对有 runtime/scoped binding 的 plugin 强制新建。

不接受：

- 仅调用 `filtered_copy()` 后继续对共享 plugin 调用 `bind_agent_tool_runtime()`；
- 使用 `copy.copy()` 但未验证内部 mutable fields；
- 依赖“父 Agent 通常不并行”规避竞态。

最小要求：

```text
parent registry identity != child registry identity
parent plugin identity != child plugin identity for mutable plugins
bind child runtime -> parent runtime unchanged
bind child scoped tools -> parent authorization/file state unchanged
parallel parent + child calls remain correctly routed
```

`ToolRegistry.filtered_copy()` 是否改变为真正 clone 属于实现选择，但不得破坏其他调用方对 registry copy 的既有语义；若全局修改风险较大，可新增 child-only factory。

### 4.4 统一 child result envelope 与 review auto-advance

本规格依赖并衔接 `docs/design/subagent-report-protocol.md`，但不要求一次性实现该文档的全部通用 schema。最小闭环要求如下。

#### 4.4.1 Delegation metadata

`agent` spawn 的 ToolResult metadata 必须保留 delegation mode，而不是只保留 public agent identity：

```json
{
  "agent": "voidx",
  "mode": "review",
  "run_id": "run_...",
  "status": "running"
}
```

`agent` identity 与 `mode` 语义不可混用。

#### 4.4.2 Terminal result metadata

child terminal result 固化在 `AgentRun.result` 中，至少需要能被父层识别：

```json
{
  "result": "verdict: FAIL\nfindings: ...",
  "mode": "review",
  "verdict": "FAIL",
  "status": "complete|incomplete|failed",
  "finish_reason": "final_answer|message_result|..."
}
```

实际字段命名可与结构化汇报协议最终类型统一，但必须满足：

- `mode` 可判定；
- review `verdict` 可枚举判定；
- terminal/incomplete/failed 可区分；
- `output` 文本仍可兼容旧父层和 UI；
- `AgentRun.result` 是唯一权威结果，不依赖 inbox 中是否同时存在 `result` 和 lifecycle 消息。

#### 4.4.3 父层统一消费

以下来源必须归一为相同的 `executed tool result` 语义：

1. 直接同步 child result 的 legacy adapter；
2. `agent_control(wait)` 返回的 terminal snapshot；
3. gateway terminal lifecycle 触发后父层刷新 child snapshot。

`auto_advance_events()` 不应只绑定 `tool_name == "agent"`。推荐新增纯函数形式：

```python
review_event = review_result_event(
    mode=..., output=..., metadata=..., run=...
)
```

它应优先读结构化字段，只有在明确标记 legacy 时才使用兼容 regex。兼容 regex 至少同时支持 `verdict: FAIL` 与 `verdict=FAIL`，但新路径不得依赖正则。

#### 4.4.4 review 路由行为

- review child 返回 `PASS`：父层可以结束当前 review route，不能自动进入 feedback。
- review child 返回 `FAIL`/`NEEDS_CHANGE`：父层产生既有 `review_has_issues` 事件；是否进入 feedback 仍由父 route/route-terminal 规则决定。
- review child incomplete/failed/timeout：不得伪装成 review verdict；父层得到可操作的 incomplete/failed 状态并决定重试、缩小任务或报告阻塞。
- 子 Agent 不直接调用父 `workflow`，也不直接激活 feedback。

### 4.5 普通消息通道的闭环选择

实现必须在以下两个方案中明确选择一个，不能保持当前半闭环状态。

#### 方案 A：V1 收窄为终止结果（推荐）

- child LLM surface 只暴露 `message(result)`，或移除 message tool，统一使用自然最终答案/terminal result。
- `question`、`answer`、普通 `message` 保留 transport/domain 兼容能力，但不向当前 child LLM 暴露。
- 父 Agent 使用 `agent_control(wait)` 取得结果。
- 这是本次实现的默认方案，风险最小。

#### 方案 B：实现完整双向交互

只有在确有需求时采用：

```text
child question -> parent inbox/lifecycle wakeup
parent LLM sees structured question -> parent answer message
child run resumes with answer in next context
```

必须新增：

- parent inbox 消费与去重；
- 父 LLM 上下文注入规则；
- child resume/wakeup 状态机；
- question timeout/cancel 语义；
- answer 与 question correlation id；
- 测试覆盖 child 发送 question、父回答、child 继续和最终 result。

在方案 B 完成前，禁止在 child prompt 中声称可进行可靠的交互式问答。

### 4.6 general/meta 请求与 workflow 继承

引入明确的 workflow inheritance decision，不能仅依赖 `TaskIntent.GENERAL` 与 active run 的组合猜测。

目标行为：

- 新的 general/meta 请求默认不执行旧 workflow gate、不自动推进旧 workflow、不把旧 workflow persona 当作当前请求 persona。
- 父层可以显示旧 workflow 处于 paused/background 状态，但必须和当前 task state 分离。
- 用户明确表达“继续上一个任务/继续实现/处理该 review 反馈”时，才恢复旧 goal/workflow。
- 恢复时必须在当前上下文中显式标记：`Workflow inheritance: resumed from ...`。
- 不得为了实现此行为删除持久化 workflow history；只改变当前 turn 的 active context 选择。

可接受实现包括在 `TaskState` 增加 `workflow_context_mode = active|paused|none`，或在 turn preparation 中生成等价的 typed decision；禁止只靠字符串检查 `general`。

## 5. 生命周期与错误路径

| 场景 | 目标行为 |
|---|---|
| child 正常最终答案 | `AgentRun.result` 固化；父 `wait` 取得完整结果；父 LLM 下一轮可消费 |
| child 调用 `message(result)` | result 先固化 run，再发送 best-effort 通知；inbox 压力不能把 child 变成 failed |
| child 返回 review FAIL/NEEDS_CHANGE | 结构化 review event 进入父 auto-advance；不依赖同步 spawn output |
| child 返回 `verdict=FAIL` legacy 文本 | legacy parser 识别并记录兼容路径；新结果继续使用结构化字段 |
| child context overflow/time/guard terminate | `status`/`finish_reason` 明确为 incomplete；父不得当作 PASS 或完整 review |
| `agent_control(wait)` 超时 | child 仍 running；父得到已有状态和可操作 hint；不自动 cancel |
| `agent_control(wait)` 已 terminal | 返回 `AgentRun.result` 权威内容；不要求 lifecycle 和 result 两条消息都存在 |
| child 发送普通 message/question | V1 默认不在 child surface 暴露；若采用方案 B，必须进入 question/answer 状态机 |
| child debug 尝试 write | 工具不应出现在 surface；即使手工伪造 tool call，authorization/dispatcher 仍拒绝 |
| child bind runtime | parent plugin runtime、authorization、file state、invoker 完全不变 |
| 父 Agent 新 general/meta 请求 | 旧 workflow paused/不参与当前 gate，除非用户明确恢复 |
| 子 Agent 无 project instructions | 明确标记 handoff 缺失，不假称已遵守未知约束；仍可通过 read 触发动态 instruction resolver |
| parent/child 并行执行同一 plugin id | 两侧使用不同实例和 scoped runtime；结果、权限和消息路由互不覆盖 |

## 6. 数据模型与兼容性

### 6.1 不需要的迁移

本规格不要求数据库 schema migration。现有 `AgentRun`、gateway lifecycle、`WorkflowRoute` 和标准 child route 保持可反序列化。

### 6.2 允许的模型增量

若结构化结果协议尚未落地，可先增加以下非破坏字段：

- delegation `mode`；
- terminal result `status`；
- terminal result `verdict`（review 时）；
- `finish_reason`；
- context handoff source metadata。

字段必须有旧数据默认值，旧 run 中缺失字段不得导致恢复失败。

### 6.3 兼容要求

- `run_subagent()` 继续接受调用方显式传入的历史/非标准 route；不能把 legacy `tdd -> verify` 误改成标准 `tdd -> tdd`。
- parent `agent` 工具的公开输入仍是 `review | debug | implement`。
- parent registry 继续保留 `agent`、`agent_control`、`workflow` 等工具。
- child registry 不得把 `message` 或其他 child-only binding 泄漏回 parent。
- 既有 `agent_control(wait|cancel)` route、有限等待、terminal priority 和 inbox pressure 语义保持不变。
- `AgentRun.result` 文本兼容旧调用方；新增结构化 metadata 不得要求所有 UI 同时迁移。
- auto-advance 的 legacy regex fallback 只能作为过渡兼容，必须有 telemetry/test 标记其使用，不得成为新结构化路径的唯一来源。

## 7. 禁止修改范围

以下内容除非本规格明确点名，不得修改：

- `DEFAULT_WORKFLOW_DAG`、内置节点语义和主 Agent workflow 工具；
- 主 Agent 的 route-terminal 逻辑；
- `AgentInput` 的 mode enum 和公开 delegation 参数；
- 既有 `AgentResultContract` preset 的字段名/公开 format（结构化协议单独批准的兼容扩展除外）；
- gateway 的跨 session/sibling route policy；
- `AgentRun` 的 terminal status 基本语义；
- 无关的 frontend、desktop、tui 和用户 dirty changes；
- 与本规格无关的 prompt、权限或工具重构。

## 8. 实现任务

每项任务必须遵循“先写失败测试，再写最小实现，再跑目标测试”的 TDD 顺序。

### Task 1 — 建立 child plugin 实例隔离

**源文件/入口**：

- `src/voidx/tooling/application/registry.py`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
- `src/voidx/tooling/adapters/scoped_plugin.py`
- `src/voidx/agent/adapters/tools/plugins.py`

**动作**：

- 设计 child-specific registry factory 或 plugin clone contract。
- 对 agent-owned、file-scoped、shell-scoped 和其他含 runtime/state 的 plugin 创建 child instance。
- 保证 child runtime/scoped binding 不改 parent instance。
- 对不可变纯定义允许共享，但写出可测试的分类规则。

**必须覆盖**：

- parent/child plugin identity；
- parent runtime binding 前后值；
- parent/child 并行执行；
- child permission snapshot 和 file state 隔离；
- child-only message plugin 不泄漏 parent。

### Task 2 — 建立 mode capability policy

**源文件/入口**：

- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
- `src/voidx/agent/adapters/langgraph/runtime/tool_surface.py`
- `src/voidx/agent/adapters/langgraph/runtime/permission_flow.py`
- `src/voidx/agent/application/prompts.py`
- `src/voidx/agent/application/runtime_context.py`

**动作**：

- 添加 mode 到 tool capability 的单一映射。
- 在 tool surface 移除 debug/review 不应看到的工具。
- 在 dispatcher/authorization 增加第二道硬拒绝，防止手工伪造 tool call 绕过 surface。
- 将 persona 文案与实际 capability 对齐。
- 从 child surface 移除 `agent_control`，除非实现方案 B 的嵌套控制协议。

**必须覆盖**：

- 三种 mode 的可见 tool ids；
- debug/review 的 write/manage/replace 和写副作用 shell 均不可执行；
- implement 仍可在现有权限层执行允许的修改；
- `agent`、`clarify`、`checkpoint`、`workflow`、`agent_control` 均不在 child surface；
- parent surface 不变。

### Task 3 — 统一异步 child result 与 review auto-advance

**源文件/入口**：

- `src/voidx/agent/adapters/tools/subagent.py`
- `src/voidx/agent/adapters/tools/subagent_control.py`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
- `src/voidx/agent/application/automation/workflow/auto_advance.py`
- `src/voidx/agent/adapters/langgraph/runtime/tool_executor/workflow.py`
- `src/voidx/agent/domain/subagent.py`

**动作**：

- 在 spawn metadata 保存 `mode`。
- 定义 terminal result normalization，优先结构化字段，保留 legacy text fallback。
- 让 `agent_control(wait)` 的 terminal snapshot 能被父层 auto-advance 识别。
- review parser 同时处理 structured verdict 和 legacy `:`/`=` 文本。
- 明确 incomplete/failed/timeout 不得生成 PASS/FAIL review verdict。
- 保持 `AgentRun.result` 为权威结果，避免依赖 inbox 通知顺序。

**必须覆盖**：

- `agent(review)` spawn 后 wait 完成并返回 FAIL；
- wait 结果驱动 `review_has_issues`；
- `NEEDS_CHANGE` 同样驱动；
- PASS 不进入 feedback；
- `verdict=FAIL` 和 `verdict: FAIL` legacy fallback；
- `agent="voidx", mode="review"` 不再丢失 mode；
- incomplete/failed 不误触发 review event；
- result/lifecycle 任一通知缺失时，父 wait 仍可取得结果。

### Task 4 — 收窄或完成 child message protocol

**源文件/入口**：

- `src/voidx/agent/adapters/tools/subagent_message.py`
- `src/voidx/agent/domain/subagent.py`
- `src/voidx/agent/adapters/subagent/inprocess_gateway.py`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
- `src/voidx/agent/adapters/tools/subagent_control.py`

**默认动作（方案 A）**：

- child LLM surface 只保留 terminal `message(result)`，或统一走自然最终答案；
- 普通 `message/question/answer` 保留 transport 兼容但不对 child LLM 暴露；
- 文案不得宣称当前 child 可进行可靠双向问答。

**若选择方案 B，额外动作**：

- 增加 inbox 消费、correlation id、父 LLM 注入、answer 回传、child resume 和 timeout 状态机；
- 通过端到端测试证明 question → answer → resume → result 完整闭环。

### Task 5 — 实现显式 context handoff

**源文件/入口**：

- `src/voidx/agent/adapters/langgraph/execution.py`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
- `src/voidx/agent/application/runtime_context.py`
- `src/voidx/agent/application/instruction.py`
- `src/voidx/agent/domain/prompt_contracts.py`（若 handoff 使用现有 ContextSection）

**动作**：

- 定义并传递 child-specific `instructions/profile_sections/summary` handoff。
- 只传递本次 delegated scope 必需的信息，记录 source paths 或等价 provenance。
- 不传递 parent transcript 和 parent 私有 workflow state。
- 保持 child 通过 read 触发动态 AGENTS.md 注入的现有能力。

**必须覆盖**：

- child 收到允许传递的 project instruction；
- 未传递 parent 私有历史；
- instruction/profile section 顺序稳定；
- 缺失 handoff 时有明确状态，不伪造已遵守规则；
- context cache 在 handoff 变化时失效/重建。

### Task 6 — 分离当前 general/meta turn 与旧 workflow

**源文件/入口**：

- `src/voidx/agent/domain/task/state.py`
- `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py`
- `src/voidx/agent/adapters/langgraph/runtime/core/context.py`
- `src/voidx/agent/application/runtime_context.py`

**动作**：

- 引入 typed workflow inheritance decision 或等价显式状态。
- general/meta 新请求默认暂停旧 workflow，不使用旧 persona/gate 作为当前请求规范。
- 用户明确恢复旧任务时显式恢复并渲染 provenance。
- 保留旧 workflow history，避免 destructive reset。

**必须覆盖**：

- active feedback + 新 general/meta 请求；
- 新请求不显示为当前 feedback gate；
- 明确“继续实现/继续上个任务”恢复 workflow；
- 恢复后 route、persona 和 transitions 一致；
- 普通 coding 请求的既有 workflow 行为不回归。

## 9. 测试计划

### 9.1 新增/修改的定向测试

建议文件：

```text
src/tests/test_tooling/test_registry_isolation.py
src/tests/test_agent/adapters/langgraph/runtime/test_subagent_tool_surface.py
src/tests/test_agent/adapters/langgraph/runtime/test_subagent_result_handoff.py
src/tests/test_agent/adapters/langgraph/runtime/test_subagent_message_protocol.py
src/tests/test_agent/adapters/langgraph/runtime/test_subagent_context_handoff.py
src/tests/test_agent/adapters/langgraph/runtime/test_general_workflow_inheritance.py
```

如仓库已有等价测试文件，必须扩展既有文件，不另建重复测试层。

### 9.2 必须执行的命令

每项任务至少运行对应 focused suite：

```bash
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime src/tests/test_agent/adapters/tools src/tests/test_agent/adapters/subagent
```

工作流与状态回归：

```bash
./test.py --backend -- \
  src/tests/test_workflow/test_auto_advance.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_workflow_done.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_workflow_review.py \
  src/tests/test_application/test_runtime_context_builder.py \
  src/tests/test_tooling/test_state_update_from_executed_tools.py
```

上下文/静态检查：

```bash
./python.py -m compileall -q src/voidx
./git diff --check
```

最终回归：

```bash
./test.py --backend
```

### 9.3 LLM-visible contract assertions

测试不能只断言 Python state；至少要捕获传给 fake model 的 system/task messages 和 tool definitions，并断言：

- mode、persona、workflow node、route 和 result contract 相互一致；
- debug/review 不见不可用或越权工具；
- child 不见 `Workflow transitions [...]`、parent transcript 和 parent-only lifecycle tools；
- child project instructions 的来源和内容符合 handoff policy；
- parent wait 后的完整结果进入 parent ToolMessage/下一轮 LLM context；
- review structured/legacy result 触发同一 workflow event；
- incomplete/failed/timeout 不被模型或 runtime 当作完整成功结果。

## 10. 验收标准

实现只有同时满足以下条件才可标记完成：

### A. 隔离

- [ ] parent/child mutable plugin instance 不共享。
- [ ] child runtime/scoped binding 不改变 parent runtime、authorization、file state 或 invoker。
- [ ] parent/child 并行工具调用的 run_id、结果和权限均正确。

### B. 上下文与工具面

- [ ] review/debug/implement 的 visible tools 与 capability policy 一致。
- [ ] debug 的 write/replace/manage 和写副作用 shell 在 surface 和硬执行层均被阻止。
- [ ] child 不见 `agent`、`clarify`、`checkpoint`、`workflow`、`agent_control`。
- [ ] parent 仍保留其既有 orchestration surface。
- [ ] child context 只显示自身单节点/显式 route，不显示父 DAG 出口。

### C. 结果与 workflow

- [ ] spawn metadata 保留 delegation mode。
- [ ] `agent_control(wait)` terminal result 能被父层消费。
- [ ] review PASS/FAIL/NEEDS_CHANGE 的结构化结果和 legacy 文本兼容路径行为明确且有测试。
- [ ] review FAIL/NEEDS_CHANGE 可靠产生 `review_has_issues`，并按既有父 route 决定 feedback。
- [ ] incomplete/failed/timeout 不产生伪造 verdict。
- [ ] inbox/lifecycle 通知缺失或顺序变化不影响 `AgentRun.result` 权威读取。

### D. 消息与上下文 handoff

- [ ] child 普通消息要么不可见，要么已有完整 question/answer/resume 闭环。
- [ ] project instructions/profile handoff 可追踪、可测试且不泄露 parent transcript。
- [ ] 新 general/meta 请求不会继承旧 workflow gate；明确恢复请求仍可恢复。

### E. 验证

- [ ] 所有 focused tests 通过。
- [ ] `compileall` 和 `git diff --check` 通过。
- [ ] `./test.py --backend` 通过。
- [ ] 最终报告列出实际修改文件、测试命令、结果和未解决风险。

## 11. 风险与开放决策

### 11.1 plugin clone 成本

不同 plugin 可能持有外部 client、缓存或 callback。实现者必须先盘点 mutable/runtime-bound plugin，再选择 factory/clone 策略；不能对所有 plugin 盲目 `deepcopy`。如果发现某 plugin 无法安全 clone，应把它改为显式 child-scoped adapter，而不是共享实例。

### 11.2 review structured report 的迁移顺序

`docs/design/subagent-report-protocol.md` 已提出通用 `AgentStructuredReport`，本规格只要求最小字段闭环。若完整协议先于本规格实施，应复用其 schema；若本规格先实施，必须保留向完整协议迁移的字段兼容性。

### 11.3 shell 只读判定

“debug 可运行无写验证命令”依赖现有 shell 风险分类是否足够精确。若不能可靠判断，宁可从 debug surface 移除 shell，也不能把只读保证留给模型自律。

### 11.4 project instructions 的范围

传递过多会扩大上下文、泄露 parent policy；传递过少会导致行为不一致。第一版应采用 allowlisted sections + provenance，必要时通过 child 在目标路径 read 时动态补充，而不是复制整个父 system prompt。

### 11.5 general/meta 与 workflow 的用户意图

“审查 workflow”与“继续 workflow”可能使用相似措辞。实现应优先使用显式当前 turn intent/用户确认；无法确定时应询问一次，而不是静默推进旧 workflow。

## 12. 完成后的文档归档

本 spec 只能在以下条件满足后归档：

1. 实际实现文件存在并通过本规格最终验收命令；
2. review workflow 对实现给出证据化 PASS；
3. 再执行：

```bash
./scripts/archive.py docs/specs/subagent-context-closure-2026-09-02.md
```

在此之前不得移动到 `docs/archive/`。
