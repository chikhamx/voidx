# 事件化输出核心与无头 SDK 架构规范

> **Status: Done** — Archived on 2026-09-16.

- 日期：2026-09-13
- 状态：评审后修订稿；待设计确认与实施计划批准，尚未实现。
- 读者：架构评审者、实现者与编码代理（human + LLM）。
- 范围：`src/voidx/agent/`、`tooling/`、`bootstrap/`、`presentation/` 及拟新增的 `sdk/`。

## 1. 决策摘要

将“纯事件驱动”限定为**面向消费者的输出边界事件化**：核心通过模型、工具、存储、交互等端口执行业务，通过结构化语义事件报告结果。事件不替代全部业务端口，内部诊断日志不强制进入 SDK 事件流。

第一阶段交付可独立运行、可取消、可关闭的无头 SDK，复用现有消息和执行状态持久化。随后用兼容投影器接入终端与 Gateway，验证后移除核心的命令式 UI 分支。展示历史的事件化持久化是独立后续项目，不作为 SDK 首版前置条件。

### 1.1 当前事实与问题边界

| 当前事实 | 源码依据 | 本规范处理方式 |
| --- | --- | --- |
| UI 端口仍暴露 dock、console、刷新控制与 `via_events()` | `src/voidx/agent/ports/ui.py` | 从核心执行路径逐步移除，替换为语义事件 |
| 已有同步语义事件端口和另一套 UI 事件模型 | `agent/ports/events.py`、`agent/domain/events.py`、`agent/domain/ui_events.py`（均相对 `src/voidx/`） | 扩展期间并存并显式适配，不原地替换类型造成所有消费者同步失效 |
| 执行状态与展示快照已经分开 | `src/voidx/agent/adapters/langgraph/runtime/session_runtime.py` 的 `restore_runtime_state` 与 `restore_transcript_snapshot`；默认使用 `NullPresentationSnapshotPort` | 保留已有分离，不把所有会话恢复重写为事件回放 |
| 模型消息可直接从仓库加载 | `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`、`src/voidx/agent/adapters/persistence/session_repository.py` | 继续作为执行历史来源 |
| 展示历史仍以 OutputTree 行数据保存 | `src/voidx/presentation/adapters/persistence/transcript_snapshot.py` | 首版保留格式；后续独立设计投影迁移 |
| 交互已存在纯数据 DTO 与请求端口 | `src/voidx/tooling/domain/interaction.py`、`src/voidx/tooling/ports/interaction.py` | 复用边界、扩展原因与校验语义，不另建冲突的响应类型 |
| Gateway 同时处理 UI 事件与交互 RPC | `src/voidx/presentation/gateway/session/core.py`、`session/method/sessions.py` | 兼容范围覆盖整个事件投影，不只覆盖 `ui.request` |

上述分离不代表当前整个运行时已能无头启动；无头装配、执行路径和导入副作用仍需合同测试证明。桌面 IDE、剪贴板等能力只在明确请求且宿主支持时由可选适配器提供，不能成为无头启动依赖。

### 1.2 目标与非目标

目标：
- SDK 不装配终端、Rich Console、Dock 或 Gateway，支持流式消费和明确的生命周期管理。
- 终端/Web/Desktop 保持已有协议与展示行为；兼容是需要测试证明的验收目标，不是仅凭 Schema 不变即可成立的承诺。
- 无消费者的内部运行使用丢弃输出的发布端口；交互仍按显式策略结束，不因缺少 UI 抛异常。

非目标：
- 不修改 LangGraph 核心编排，不将执行状态重建改成 event sourcing。
- 不修改 SQLite Session 表结构，不替换现有消息仓库与 transcript 文件格式。
- 不在 SDK 首版提供跨进程恢复 pending Future、事件日志持久化或可靠重放 API。
- 不改变终端视觉设计，不删除平台层可复用的非 GUI 能力。

## 2. 架构边界与不变量

1. `src/voidx/bootstrap/` 是唯一跨功能具体适配器组合根，遵守 `src/AGENTS.md`。拟新增 `bootstrap/headless.py` 负责无头装配，不能先实例化终端再关闭它。
2. `sdk/` 只组合公共配置、DTO 与 headless 工厂；不得直接构造图节点、仓库、MCP/LSP 管理器。依赖配置与 DTO 合法，不应断言 SDK 只能 import 一个模块。
3. 核心保留模型、存储、工具、交互等业务端口；最终 `agent/`、`tooling/` 不导入 `voidx.presentation`、`voidx_cli` 或 `rich`。扩展阶段的既有债务不能以新增依赖扩大。
4. 核心可观察输出统一发布语义事件，不直接控制外部 UI 的 `invalidate`、`update_status`、输入框或渲染树。禁止的是 UI 控制调用，不是所有同名业务状态方法。
5. 内部诊断继续使用 `voidx.observability`。面向用户的警告/错误发事件；诊断日志不受慢消费者背压控制，不自动暴露密钥、内部堆栈或原始请求。
6. 事件消费者不得通过修改载荷改变执行状态。所有响应通过显式交互端口提交。

目标数据流：

```text
SDK -> bootstrap/headless -> runtime -> 模型/工具/存储/交互端口
                                |
                                v
                      await SemanticEventPublisher.publish
                                |
                       每次运行的有界事件通道
                                |
                 SDK 消费者 / 兼容 UI 投影器
                                |
                   既有终端与 Gateway 协议
```

## 3. 语义事件合同

### 3.1 类型归属与迁移

扩展阶段在 `src/voidx/agent/domain/semantic_events.py` 新增 `SemanticEvent` 判别联合；在 `agent/ports/events.py` 新增异步 `SemanticEventPublisher`，暂不改变已有同步 `EventPublisher.publish`。相关新路径均为计划创建，不能视为已存在。

收缩阶段仅在全部调用方完成迁移后移除旧语义发布接口；旧 UI DTO 可以暂时保留为兼容协议，迁往 presentation 的动作须保持其公开重导出与协议 Schema。不要求为了命名统一再将新文件改名。

### 3.2 公共信封

每条事件具备以下字段：

| 字段 | 合同 |
| --- | --- |
| `schema_version` | 首版固定为 1；未知版本拒绝解析，不静默猜测 |
| `event_id` | 全局唯一，不用时间戳充当 ID |
| `session_id`、`thread_id`、`turn_id` | 运行建立后非空；一次 SDK stream 对应一个顶层 turn |
| `sequence` | 同一顶层 turn 内从 1 开始严格递增；子代理事件共享该运行的序号分配器 |
| `agent_id`、`parent_tool_call_id` | 关联代理与父工具；没有父级时显式为空 |
| `timestamp` | UTC Unix 时间，仅用于展示，不用于排序 |
| `kind`、`payload` | 判别联合，必需业务数据使用具名字段，不用任意 metadata 替代合同 |

发布时校验并生成独立的 JSON 快照；消费者获得独立解码对象，不与生产者或其他消费者共享可变容器。`frozen=True` 仅防字段重绑定，不能作为深层不可变保证。拒绝不可序列化的 `Any` 对象。

首版单事件 JSON UTF-8 上限 256 KiB，默认队列 256 条；两者均需校验为正数。流式文本按编码安全边界拆分；大工具结果或 diff 使用结果存储引用及有界摘要，禁止静默截断语义字段。引用沿用现有结果存储授权及清理边界，不自动持久化到展示事件日志。超过上限且不能引用的载荷作为明确的发布失败终止 turn。

### 3.3 事件族与最小业务字段

| 事件族 | 必需内容与关联 |
| --- | --- |
| `turn.started/completed/failed/cancelled` | 输入文本与既有 TurnMetadata；完成携带 usage；失败携带脱敏错误码、摘要和 recoverable；取消携带原因 |
| `assistant.stream_started/chunk/committed/discarded` | `stream_id`、`phase`；chunk 为 delta；committed 给出最终文本或结果引用；discarded 标识放弃的草稿 |
| `tool.started/finished/result` | `tool_call_id`、名称、经脱敏的参数；耗时、成功状态；结果摘要及可选引用 |
| `file.changed` | 工具关联、路径、diff 或引用 |
| `status.started/updated/finished` | 稳定 `status_id`、业务阶段、描述、父工具关联及结束结果；不携带刷新命令 |
| `todo.updated/committed/cleared` | 完整条目快照、操作类型与提交边界，支持旧 UI 的预览/提交区别 |
| `interaction.required/resolved` | 第 5 节定义的请求、业务用途和解析结果 |
| `subagent.started/step_started/finished` | 子代理 ID、父级关系、描述、结束原因与摘要 |
| 用户可见诊断及上下文事件 | 警告/错误、上下文压力及压缩结果、guidance 提交/生效等既有业务事实 |

本表是事件族约束，不是删除未列出现有行为的许可。进入每条输出链路迁移前，必须清点 `agent/domain/ui_events.py` 中该链路的所有事件及其消费者，并为遗漏事件补充具名 DTO 与映射测试。

顺序约束：turn started 在业务事件前；每个工具/stream/status 的开始在对应结束前；每个已开始的 turn 恰有一个终态，且终态之后不再发布该 turn 的事件。不同工具、思考和文本允许合法交错，不要求“先工具再文本”。

## 4. 发布、背压与 SDK 生命周期

### 4.1 异步发布与运行完成通道

新端口合同为 `async def publish(self, event: SemanticEvent) -> None`。运行时必须 await 发布，使用 `asyncio.Queue(maxsize=...)` 背压；首版不丢弃、不合并事件。不允许通过为每次 put 创建 Task 或另设无界缓冲绕开背压。同步回调链路必须先迁移至可等待边界，不能假装同步旧端口具备异步背压。

每个运行拥有：一个后台执行 Task、一个有界业务队列、一个独立的 completion Future 和一个监督者。监督者是唯一终态写入者；执行 Task 返回结构化结果，不直接投递终态。

- 正常/失败结束：监督者完成必要持久化与交互清理，在队列之外保存唯一终态并完成 completion Future。迭代器先排空已接受事件，再 yield 终态并结束。
- 队列空时，迭代器同时等待新事件与 completion；后台失败不会让 `queue.get()` 永久挂起。
- 取消不需要向已满队列 put sentinel 或终态；pending put 可被取消。监督者先停止并等待所有生产者，再把尚未输出的交互终结信息转存到 completion 的有界尾部（最多 64 条），最后确定 turn 终态。迭代器依次输出已接受队列、交互尾部和终态；序号按此顺序分配。仅对已成功发布 required 的请求输出 resolved，不产生孤立响应事件。
- `NullSemanticEventPublisher` 立即返回，不积压输出。配置此端口的内部无监听运行，与调用 stream 后不读取，是不同场景；后者按慢消费者处理。
- 多消费者分发属于适配层，不能让一个未消费的额外订阅者无限积压。持久化不依赖 SDK 消费者是否及时读队列。

### 4.2 SDK 公共行为

拟新增 `src/voidx/sdk/agent.py` 与 `sdk/__init__.py`，公开 `VoidxAgent`。配置复用 `voidx.config.Config` / `Settings` 现有体系；不假设存在 `VoidxConfig` 或另造凭证存储。

接口合同：
- `stream(prompt, *, session_id="", workspace=".") -> AsyncIterator[SemanticEvent]`：惰性开始运行；首次迭代时占用运行槽，建立 session/turn 后发布 started。
- 一个实例只允许一个活动 stream；第二个 stream 首次迭代时抛 `RuntimeError`，不得串改 workspace/session。需要并发时使用多个实例；底层仍遵守现有会话和工作区写锁。
- `submit_interaction(interaction_id, response) -> bool`：异步方法；只完成响应 Future，不 await 发布事件或执行完成，避免在消费循环中死锁。
- `cancel(*, session_id="")`：取消并等待当前运行和其受管子任务结束；空 ID 指当前运行，不匹配活动 session 时抛 `ValueError`；空闲时幂等返回。
- `aclose()`：幂等；先阻止新运行，再取消并等待活动运行、交互与受管任务，最后关闭本实例拥有的资源。共享资源不得由单实例误关闭；资源关闭失败显式报告，不吞异常。
- 关闭后 stream 与 submit 抛 `RuntimeError`；cancel 无操作返回。模型/工具运行失败通过 `turn.failed` 表达；初始化或 API 使用错误在 started 前直接抛异常。

`stream` 的 finally 必须清理后台执行。普通 `async for` 的 break 不保证立刻关闭异步生成器，提前退出必须使用 `contextlib.aclosing` 或关闭 agent，不能把垃圾回收当生命周期机制：

```python
from contextlib import aclosing
from voidx.sdk import VoidxAgent

async with VoidxAgent(config) as agent:
    async with aclosing(agent.stream("检查工作区")) as events:
        async for event in events:
            if should_stop(event):
                break
```

示例中的 `config` 与 `should_stop` 由调用方提供。消费者继续迭代可观察取消终态；主动关闭迭代器/agent 后不承诺该消费者还能收到事件，但内部必须记录唯一运行终态。SDK 不提供无消费自动跑完的隐式后台模式。

## 5. HITL 交互合同

### 5.1 数据与注册表

扩展 `src/voidx/tooling/domain/interaction.py`，保留现有 `UserResponse(value: str, cancelled: bool, free_text: bool)` 兼容入口；不另建 `agent/domain/interaction.py`。新增具名解析结果，区分外部响应和内部决策原因，不把 `None` 直接传给现有必填字符串字段。

请求至少包含：
- `interaction_id`、session/thread/turn 归属；
- `input_kind: choice | text | permission`，描述输入形式；
- `purpose: permission | checkpoint | clarify | goal | loop | generic`，描述业务用途；
- prompt、具名 choices、是否允许自由输入、text 默认值和保密标记；
- permission 的工具详情与允许 scope；checkpoint/goal/loop 的结构化业务载荷；
- 有限正数 timeout，默认 120 秒；明确业务配置可覆盖，首版不允许无限等待。

解析结果包含 `value`、`free_text`、`decision` 与 `resolution_reason`。原因至少区分 `answered`、`user_rejected`、`dismissed`、`timed_out`、`task_cancelled`。`cancelled` 兼容字段不能继续单独决定所有业务降级。

注册表由运行时交互协调器持有，不由 SDK 和 Gateway 各自维护一份 Future。首版每个运行最多 64 个 pending 请求，超限作为明确运行错误；SDK 门面仅转发。注册先于发布 required，超时自 required 成功入队后开始计时；预算包含队列等待被消费者读取的时间，不承诺实际展示满 120 秒。

### 5.2 响应与竞态

- 校验 ID 归属、选项、自由文本许可和 permission scope，禁止只凭字符串 ID 向任意运行提交响应。
- 非法响应抛 `ValueError` 且保留 pending；未知、重复或已超时 ID 返回 False。
- 回答、超时、取消竞争时，只允许第一次状态迁移生效；在 finally 移除 Future 与计时器。
- resolved 由协调器产生，submit 只唤醒 Future；保证一次解析、一次 resolved。
- 取消先把运行标记为 cancelling，使所有决策分支停止推进，再取消并等待执行 Task；不得先注入普通 cancelled 响应让 goal/loop 进入自动批准。

### 5.3 决策策略

| purpose | 超时或关闭交互窗口 | 用户明确拒绝 | 主动取消运行 |
| --- | --- | --- | --- |
| permission | deny | deny | 终止，不执行工具 |
| checkpoint | rejected | rejected | 终止，不越过检查点 |
| clarify | skipped，记录未获得答案；不得当作授权 | skipped | 终止 |
| goal / loop | SDK 默认 rejected；仅显式启用旧自主模式兼容策略时 auto_approved | rejected | 终止，禁止 auto_approved |
| generic | cancelled，无隐式批准 | rejected | 终止 |

现有 `agent/adapters/tools/automation/goal.py` 和 `loop.py` 将部分 cancelled 响应自动批准；终端/Gateway 的旧模式兼容由 bootstrap 显式配置，并以回归测试固定。安全权限与 checkpoint 不因兼容策略放宽。

## 6. 终端与 Gateway 兼容投影

拟新增 `src/voidx/presentation/adapters/semantic_event_projector.py`，将语义事件转换为既有 UiEvent；Gateway 继续走既有 `broadcast_event` 与 v2 投影，不直接把新模型发给前端。

| 新语义信息 | 旧协议/事件 | 必须保留的行为 |
| --- | --- | --- |
| turn 生命周期 | TurnStarted/Completed/Failed/Cancelled | 输入文本、raw_text 语义、TurnMetadata、错误字段映射 |
| stream 生命周期 | AssistantStreamStarted/Updated/Committed/Discarded | stream_id；delta 转累计或明确声明 delta；取消草稿不误提交 |
| tool/result/file | ToolStarted/Finished、ToolResultAppended、FileChangeAppended/DiffAppended | 调用关联、摘要与详情；展示折叠/行数等由 presentation 既有策略决定 |
| status 生命周期 | StatusUpdated/Finished | status_id、stage、父工具、结束与移除行为 |
| todo 生命周期 | TodoUpdated/Committed/Cleared | 预览、提交与清空边界 |
| interaction required | UiChoiceRequest/UiTextRequest/UiPermissionRequest 加对应业务 prompt 事件 | choices、default、保密标记、权限 tools，以及 checkpoint/goal/loop/clarify 卡片数据 |
| interaction resolved | 对应 decision/answer/cleared 事件 | 自由输入、拒绝、超时与提示清理 |
| subagent、诊断、压力与 guidance | 现有对应 UiEvent | 父子关系、计数和完整展示状态迁移 |
| UI 本地行为 | Capture/Refresh/Reset/Startup/Input/Notice | 由 presentation 自身生成，不从核心伪造 UI 命令 |

该表规定映射族。每次迁移必须枚举被迁移事件的全部字段和默认值，测试未覆盖的族继续走旧链路；不得默默丢弃未知事件，不能双重发布导致重复渲染。

交互桥接：
1. `interaction_id` 映射 `request_id`，保留 thread 归属，发送既有 JSON-RPC `ui.request`。
2. `session.respond` 保留 `request_id`、`value`、可选 `thread_id` 及既有返回 envelope。桥接器将现有空响应/自由文本编码转换成具名响应，不改变前端编码。
3. 使用已有线程运行管理的路由规则；缺少 thread 时仅允许唯一匹配，不将响应误投另一个 session。
4. Gateway 不再为同一请求创建第二份决策 Future；断连、重连和提示清理由交互协调器状态及现有协议共同驱动，回归测试必须覆盖。

Schema、事件内容、事件顺序和实际客户端消费结果均是兼容门槛。只有这些证据齐备，才可宣称 Web/Desktop 无破坏性改造。

## 7. 持久化与后续历史投影

### 7.1 SDK 首版

执行状态继续由 `MemorySessionAdapter` / `SessionService` 保存，消息继续由现有 session repository 保存。无头装配使用 `NullPresentationSnapshotPort`，不能依赖 OutputTree 才保存执行数据。

正常完成须在现有必要持久化成功后报告 completed；持久化失败报告 failed。取消沿用现有安全中断边界，只保存有效状态，不承诺回滚已经发生的文件或外部工具副作用。

现有终端/Gateway 仍读写 `transcript.jsonl` 及索引。输出事件化不等于将这些文件替换为新事件日志。

### 7.2 独立后续项目的进入条件

未来 `TuiTranscriptProjector` 不能把 `BaseMessage` 和完整展示事件当作等价输入。消息只保证消息视图；完整交互卡片、状态过程及子代理结构需要额外展示事实。

开始该项目之前另行批准持久化规范，至少明确：
- 消息、执行状态、展示事件三者的唯一事实来源与提交边界；
- 事件版本、turn 原子性、写入去重、崩溃后不完整尾部恢复；
- 旧 transcript 双读/迁移、稳定 turn/node ID、分页游标、索引与回滚；
- 事件去重及重放不能重新执行工具、发起审批或触发桌面副作用；
- 旧数据无法重建的信息如何降级，不虚假承诺完全一致。

保留 `transcript_snapshot.py` 已有完整 turn 事务及崩溃重试去重语义，未验证前禁止删除旧格式支持。

## 8. 实施边界与阶段门槛

以下是设计级阶段，不替代经批准的逐项 TDD 实施计划。

| 阶段 | 主要文件（均相对仓库根） | 出口条件 |
| --- | --- | --- |
| A：合同扩展 | 新增 `src/voidx/agent/domain/semantic_events.py`；扩展 `src/voidx/agent/ports/events.py`、`src/voidx/tooling/domain/interaction.py` | 新旧接口可并存；事件与交互验证测试通过 |
| B：最小无头 SDK | 新增 `src/voidx/bootstrap/headless.py`、`src/voidx/sdk/agent.py`、`src/voidx/sdk/__init__.py`；在 `agent/application/` 按职责放置运行监督与交互协调器 | 无 UI 装配，真实编排配确定性模型替身可完成工具调用、权限交互、恢复与关闭 |
| C：兼容输出迁移 | 新增 `src/voidx/presentation/adapters/semantic_event_projector.py`；逐链路修改 `src/voidx/agent/adapters/langgraph/runtime/` 与 `src/voidx/presentation/gateway/session/` | 各迁移族具备字段/顺序/消费测试，未迁移链路继续使用旧路由 |
| D：收缩与交付 | `src/voidx/agent/ports/ui.py`、旧发布适配器及 bootstrap 装配点 | 所有输出迁移后删除 UI 控制依赖，架构检查与全量相关回归通过 |
| 后续：历史投影 | `src/voidx/presentation/adapters/persistence/` | 独立设计批准后才进入，不阻塞 SDK 首版 |

不得改动无关工作树内容；不得为使架构扫描通过而移动违规代码但保留同样的运行耦合；不得通过放宽断言制造兼容通过。禁止在同一个输出链路同时启用旧直写与新投影；回滚以链路装配开关恢复旧路径，不通过逆向修改已持久化的数据实现。

## 9. 验收与验证命令

### 9.1 拟新增合同测试

计划新增 `src/tests/test_sdk/`（当前尚不存在），覆盖：
- 无 TTY/PTY、无 GUI 环境、无 mock UI；确定性模型替身配真实编排及临时持久化，调用真实沙箱文件工具。
- 事件 JSON 往返、尺寸上限、载荷隔离、ID/序号、工具与 stream 交错、唯一终态。
- 队列设为 1，慢消费产生真实背压；满队列取消/关闭均在测试 deadline 内结束，无遗留 Task/Future。
- 后台异常唤醒等待者；提前 break 配 aclosing、消费 Task 取消、重复关闭、初始化失败后资源清理。
- 同实例并发拒绝、session 不匹配取消、关闭后调用与多个实例隔离。
- HITL 正常回答、非法/重复/过期响应、自由文本、scope 校验、超时与取消竞争；取消 goal/loop 不自动批准。
- 无监听 Null publisher 可完成；SDK 消费者不读时按背压暂停，而非无限积压。
- 持久化失败不发 completed；无头恢复不访问 tree；关闭后不再使用已释放资源。

新增目录后运行：`./test.py --backend -- src/tests/test_sdk -v`。预期全部通过；测试使用本地确定性依赖，不依赖付费模型或外网。

### 9.2 现有回归与架构检查

在对应阶段先运行聚焦检查，再运行广泛回归：

```bash
# 事件/交互迁移及核心执行
./test.py --backend -- src/tests/test_agent/adapters/langgraph -v
# Gateway、协议与客户端契约
./test.py --backend -- src/tests/test_presentation/gateway src/tests/test_presentation/test_protocol_schema.py src/tests/test_contracts -v
# 保持既有持久化行为
./test.py --backend -- src/tests/test_agent/adapters/persistence src/tests/test_presentation/adapters/persistence -v
# 最终分层检查；新增 AST 与运行导入检查覆盖第 2 节不变量
./test.py --backend -- src/tests/test_architecture -v
# 相关全量回归
./test.py --backend
./test.py --frontend
./test.py --desktop
```

预期各套件通过。前端/桌面运行环境不可用时须明确记录缺口，不以 backend 通过替代客户端兼容证据。适配测试还必须覆盖多线程路由、重连、permission tools、文本默认值/保密输入和复杂交互卡片。

协议 Schema 的只读一致性检查：

```bash
./python.py - <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path('src').resolve()))
from voidx.presentation.protocol import export_protocol_schema
actual = export_protocol_schema()
expected = json.loads(Path('frontend/src/rpc/protocol.schema.json').read_text())
assert actual == expected, 'UI protocol schema changed'
PY
```

预期退出码 0，且不写文件。`scripts/export_ui_protocol_schema.py` 当前没有 `--check` 选项，只能用于主动生成 Schema，不应作为只读检查调用。

### 9.3 文档与发布门槛

设计确认后另写逐项实施计划，落实协调器/监督者的准确文件路径、每项 RED/GREEN 测试与迁移链路清单。完成文档修订不等于这些测试已通过，也不等于 SDK 已实现。

只有实现文件存在、上述对应证据覆盖最终状态且未决兼容问题关闭后，才能将规范标为已完成，并按仓库规则归档。
