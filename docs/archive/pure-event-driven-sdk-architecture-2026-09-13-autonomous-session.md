# 自治会话 SDK：执行补充规范

> **Status: Done** — Archived on 2026-09-16.

- 日期：2026-09-14
- 状态：会话模式设计已由用户批准；本补充规范待实施，**不代表自治 SDK、生产迁移或 Stage D 已完成**。
- audience：human+llm。
- 主规范：`docs/specs/pure-event-driven-sdk-architecture-2026-09-13.md`。本次仅新增本文件，不修改主规范、代码、Schema、存储或默认入口，不归档。
- 质量门槛：面向人类读者检查决策与边界是否清晰；面向执行代理检查源码依据、计划路径、约束和验证命令是否明确。本文未执行功能实现或 RED/GREEN。

## 1. 决策与适用范围

SDK 根据 session 固定的 profile 执行 coding、goal 或 loop。一次 `stream` 覆盖本次自治任务的 intake、工作、评估、等待及后续真实轮次，直到自然终结、cancel 或 aclose。它不是常驻会话守护进程，也不提供脱离 stream 的后台启动、轮询或继续运行 API。

保留每个真实子轮的 session/thread/turn；不把 evaluator、work 或 loop iteration 重写成根 turn。不新增自治 execution loop，不用 fake graph 或 NullUI 拼装“无头”能力；复用现有 AgentApplication、controller、store、service、scheduler 和生产 LangGraphExecution。

本补充针对主规范 §3.2 的“一次 stream 一个顶层 turn”及 §4 的单轮 completion 作明确扩展：**stream = run，run 包含多个真实 turn；turn 终态不等于 run 完成**。主规范其余安全、序列化、资源与兼容规则继续成立。run 完成使用独立 completion，不伪造额外 `turn.completed`，也不新增 UI 协议字段。已存在的主规范计划路径以本文现场核实为准。

### 1.1 SDK 公共合同

沿用 `src/voidx/sdk/agent.py` 的 `stream(prompt, *, session_id="", workspace=".")`、`submit_interaction`、`cancel`、`aclose` 和异步上下文管理接口；不新增 profile 参数。

| 输入/阶段 | 规定行为 |
| --- | --- |
| 不提供 session | 通过现有 create_session 创建 coding session；不得由 prompt 猜 goal/loop |
| 已有 session | 加锁后重读 session；优先恢复 profile_snapshot，无 snapshot 时使用 session.runtime_profile 查 registry；不可用、损坏、不支持的 profile 明确拒绝，无 silent coding |
| 新 goal/loop | 调用方先通过既有 session 创建机制指定 profile，再传 session_id；复用现有 snapshot/关系字段，不造新 schema 或 SDK 专属存储 |
| coding | 一个普通真实 turn，持久化和清理后结束 stream |
| goal/loop idle | 真实 intake turn；未批准或只是聊天时该 turn 结束后 run 结束，不等待未来用户消息 |
| 已批准 intake | controller 提供已批准 spec，service.start 真正启动后持续消费其整个 generation，首轮终态不结束 stream |
| 已有非终态 generation | 在恢复阶段接管既有 service/scheduler 状态，不重复 init；本次 prompt 不再作为第二个 intake 投递，避免恢复和新任务混跑。调用方需先终止旧任务再提交新任务 |
| loop 等待 | 无事件可以暂时阻塞，等待由现有 outbox/pump 驱动；不是 stream 已完成。无限 loop 必须由 cancel/aclose 终结 |
| 同实例第二个 stream | 首次迭代时 RuntimeError；多个实例仍受 session/workspace 写锁保护 |
| cancel | 空 session_id 指当前 run；非空只接受根 session_id，不接受某子 session 代替根；不匹配 ValueError；空闲幂等 |
| aclose/提前退出 | 阻止新运行并等待所有受管资源终止；调用方提前 break 使用 aclosing；不依赖 GC；关闭后不承诺该消费者还能收到事件 |

初始化及恢复校验失败在首次 turn.started 前抛异常，释放已获取资源。某个真实 turn 开始后的执行错误先结束该 turn，run 清理后以失败 completion 结束；不得因为已有 intake completed 而吞掉后续错误。

## 2. 现场代码依据与差距

以下路径均为已存在文件；结论基于当前脏工作树源码，不把历史主规范描述当成当前代码。

| 已存在路径 | 已核实事实 / 本补充要求 |
| --- | --- |
| `src/AGENTS.md` | bootstrap 是唯一具体适配器组合根；application 面向 ports |
| `src/voidx/bootstrap/headless.py` | build_headless_run 建 session/锁；_build_session_run 直接装 LangGraphExecution；restore_session_profile 的 fallback 硬编码 coding，context 的 thread_id=session.id；只调用一次 execution.run_turn，尚未装自治 service |
| `src/voidx/agent/application/agent_profile_snapshot.py` | snapshot 优先且校验 hash；无 snapshot 按 profile_id 查 registry，不可用有诊断；应复用，不另写恢复器 |
| `src/voidx/agent/adapters/persistence/session_repository.py` | 现有 create_session、get_session、profile snapshot 与消息/goal transcript 持久化入口 |
| `src/voidx/bootstrap/agent.py` | 约 240–280 行已有 AgentRuntime（AgentApplication）、ThreadStore、GuidanceService、LoopService + LoopRuntimeScheduler、GoalService + GoalRuntimeScheduler + GoalEvaluator 装配；复用构造关系，不实例化终端 bootstrap 再关 UI |
| `src/voidx/agent/application/automation/goal/goal_idle.py` | GoalIdleTurnService 建 GoalIntakeController、TurnRequest 和 goal context，runtime.run_turn 后才 service.start |
| `src/voidx/agent/application/automation/loop/loop_idle.py` | LoopIdleTurnService 同样经过真实 intake controller 和 runtime 后启动 service |
| `src/voidx/agent/application/automation/goal/goal_service.py` | start 检查 durable INIT、建 work/evaluator sessions 与 goal thread/state；恢复走 GoalRecovery；非简单循环调用 prompt |
| `src/voidx/agent/application/automation/loop/loop_service.py` | start/materialize/ensure_session/ensure_thread/run_prompt/start_pump；resume 保留原 loop session，终态不恢复；stop 清理 wakeup |
| `src/voidx/agent/application/automation/goal/scheduler.py` | GoalRuntimeScheduler 复用 runtime dispatcher 和 evaluator；必须将实际 phase 输出接入语义流 |
| `src/voidx/agent/application/automation/loop/scheduler.py` | LoopRuntimeScheduler 执行真实 iteration、decision 与等待事件；等待输出也须语义化 |
| `src/voidx/agent/application/autonomous.py` | 共享 parent-scoped 状态、锁、deactivate/CAS 和 scheduler 合同；不复制到 SDK |
| `src/voidx/agent/application/runtime/pump.py` | WakeupPumpMixin.start_pump 当前直接 create_task；需注入受管 spawn 并提供可等待停机，不能在 SDK 外遗留任务 |
| `src/voidx/agent/application/runtime/dispatcher.py` | dispatcher 有 lease renewal 子任务；同样属于 run 的受管资源，不只管顶层 pump |
| `src/voidx/agent/application/runtime/semantic_channel.py` | SemanticChannel 校验固定 identity，有界队列、单次终态和 completion；不能向同一 channel 塞不同 child identity |
| `src/voidx/agent/application/runtime/run_supervisor.py` | RunSupervisor 已有 spawn/TaskGroup、cleanup、独立完成机制，但 RunResult/terminal 仍与单 turn 绑定；多轮须复用并拆开 run 与 turn 封口职责 |
| `src/voidx/agent/application/runtime/interaction_coordinator.py` | 单 identity，pending 上限 64，_used 随请求增长；checkpoint 有专门决策处理，goal/loop 普通合法 choice 仍可能被判 approved |
| `src/voidx/agent/adapters/tools/automation/goal.py` | _request_init_approval 缺 interaction 或 response.cancelled 会 auto_approved；approved/revised/cancelled 是实际 choices |
| `src/voidx/agent/adapters/tools/automation/loop.py` | 旧交互路径也有缺交互/取消兼容批准语义；不能直接当 semantic 安全策略 |
| `src/voidx/agent/adapters/persistence/headless_locks.py` | 文件锁按 session/workspace 排他，workspace 适配器未表达父子 owner token 和引用计数；不能独立给每个 child 重入同一 OS 锁 |
| `src/voidx/agent/adapters/langgraph/runtime/core/helpers.py` | runtime 上下文/输出绑定需支持真实 child，不得将新模式接在单根 identity 假设上 |
| `src/voidx/agent/adapters/langgraph/runtime/semantic_output.py` | SemanticOutput 是每轮输出适配边界；每个真实 turn 需独立绑定 |
| `src/voidx/presentation/adapters/semantic_event_projector.py` | 已有多 turn 投影状态；不等于 SDK 多真实 session/thread 投影和恢复已验收 |
| `src/voidx/presentation/gateway/session/core.py` | Gateway session 核心仍须验证实际子身份、输出归属及既有消费链路；不能只凭 projector 单测切换默认 |

## 3. 实现边界与执行数据流

### 3.1 责任分配（新文件均为 planned）

| 路径 | 责任 |
| --- | --- |
| `src/voidx/agent/application/runtime/autonomous_session.py`（planned） | SessionRunCoordinator：按已解析 profile 选择 coding/idle/resume；接管 service 生命周期并等待 durable 终结；不实现任务调度算法 |
| `src/voidx/agent/application/runtime/run_event_mux.py`（planned） | RunEventMux：聚合多个固定 identity 的 SemanticChannel，公平背压、活动轮注册、run completion；无模型/仓库/UI 依赖 |
| `src/voidx/agent/ports/run_lifecycle.py`（planned） | 有界 spawn/turn 注册/结束通知的类型合同，供 runtime、pump 和 bootstrap 注入；不依赖具体 supervisor |
| `src/voidx/bootstrap/autonomous_headless.py`（planned） | 无 UI 的生产 AgentApplication/ThreadStore/GuidanceService/service/scheduler/evaluator 装配与所有权注册；headless 委派到此工厂 |

改动已有 semantic_channel、run_supervisor、interaction_coordinator、headless、SDK facade、pump、scheduler、runtime 输出绑定和工具交互适配必须按下文切片进行；SDK 不直接 import 仓库/图/调度器。新的生命周期端口不是后台产品 API。

### 3.2 真实执行链路

1. SDK 惰性占运行槽；bootstrap 规范化 workspace、获取根 session 锁、重读 snapshot，验证 profile 和 workspace，建立 run owner。
2. 装配真实 execution + AgentApplication + ThreadStore + GuidanceService；按 session 模式绑定真实 automation services。semantic 输出和交互是显式能力，不注入 NullUI，也不通过 UI event stub 隐藏缺口。
3. SessionRunCoordinator 加载真实 parent thread（不能普遍假定 thread=session）；新根使用既有 runtime/store 创建机制。注册每个实际 turn 前获取有界 slot，再创建固定 identity 的 output/coordinator/channel。
4. 无 active generation 时：coding 走普通 runtime；goal/loop 走各自 IdleTurnService。模型工具必须触发实际 controller 审批提交；只有 run 仍 ACTIVE 且 spec 明确 approved 才允许 service.start。
5. service 创建的真实 child sessions/threads、GoalEvaluator、work runner、LoopRuntimeRunner 都从同一 run owner 获取输出/交互/锁能力。pump/outbox/dispatcher 执行下一阶段；coordinator 只观察其 durable 终态通知并等待，不另造 polling execution loop。
6. 每轮 runtime 完成持久化后由该 turn 的唯一 supervisor 封口。所有 generation 工作终结、pump 停止、尾部清理完成后，run supervisor 设置独立 completion。迭代器排空已接受事件及 bounded 尾部后结束。

生产服务须提供可等待的 terminal 通知（写入 durable 终态后唤醒 run owner），复用现有 state/CAS，不以 service.status 返回 None 唯独判定“成功”。成功、失败、用户停止从既有 lifecycle/decision 读取；lease 丢失、pump 意外退出和清理失败必须进入失败 completion。

## 4. 有界多轮汇聚合同

### 4.1 身份、顺序与 completion

- run_id 仅为内存生命周期标识，不混入 turn_id，不修改持久化或 UI Schema。对外仍 yield 现有 SemanticEvent。
- 每个 turn 固定真实 `(session_id, thread_id, turn_id)`；sequence 在该 turn 从 1 严格递增，event_id 全局唯一。不得将不同 child 事件改写到 root identity。
- 同一 turn 恰有一次 started 和一次 completed/failed/cancelled，终态后拒绝发布。turn_id 使用新 UUID；注册能力一次性，封口后失效，旧 handle 不能重新打开。
- 不承诺不同 turns 的因果全序；消费顺序是 mux 公平调度结果。持久化 generation/attempt/phase 的业务关联沿用既有模型，不用时间戳推导关系。
- 监督者是各自 turn 的唯一终态写入者；run owner 是唯一 run completion 写入者。run completion 是独立 Future，包含 outcome、脱敏错误摘要和有界尾部，不是额外语义 turn 事件。
- 迭代正常完成代表 run completed；run cancelled 且继续消费时，先读完真实活动轮的取消终态再结束；run failed 排空事件后由迭代器抛 RuntimeError（脱敏且稳定说明 run 失败）。无可归属 turn 的 cleanup/pump 错误只进入 run failure，不捏造 turn。

### 4.2 固定预算与公平性

首版默认：每事件 B=256 KiB、全 run 业务队列预算 Q=256、活动/尚未排空 turn 槽 T=8、全 run 未完成交互 I=64；参数必须为正，超出并发容量等待 slot 而非扩容。T 包含封口但终态尚未被消费的 channel，避免长 loop 积累 completion。现有单轮 channel 的 queue 仍可更小，但所有 child 共同受 Q 限制。

采用一个固定 mux 消费循环，对 ready child 做 round-robin，每次最多取一条再移动游标；不为每次 put 或每个等待事件新建 Task。一个 child 持续输出时，其他已 ready child 在最多 T 次选择内被服务。新轮必须先通过 T admission；同一 run 的生产者任务预算固定 P=32（包含 pump、dispatcher renewal、子代理生产者，不含固定 owner/mux）。其中为每个活动 turn 保留一个 renewal 槽，另保留两个 service pump 槽，普通执行最多 P−T−2；配置要求 P≥2T+2。调度前原子预留执行及其必要 renewal 容量，不允许执行占满槽后等待创建续租任务。父任务同步等待的嵌套子执行若无法立即获得 T/P 预算，明确容量错误而非持父槽无限等待；父任务等待期间仍计入 P。顺序后继轮可等待已封口轮被消费释放槽。所有容量等待发生在创建 coroutine/task 之前，owner 取消可唤醒 admission。

publish 顺序：检查能力与大小 → 申请全局 Q token → 在每 child 的串行发布门内生成有界 JSON snapshot 并入队 → 成功接受时分配 sequence。取消 put 必须归还 token，不能残留半条事件；消费出队释放 token。每个生产者最多持有一条大小不超过 B 的待发布快照；上游大型工具结果沿主规范使用结果引用，不能先在 mux 内聚合完整无限文本。

内存上界针对新增事件/控制缓冲：业务快照不超过 Q×B，待发布快照不超过 P×B，terminal 不超过 T×B，取消交互尾部不超过 I×B，加上 O(T+P+I) 控制项及常数份解码副本；禁止再设第二个无界 fan-out 队列、无限完成轮列表或无限错误列表。模型上下文/仓库缓存不计入事件预算，仍受其既有独立限制，不将此公式冒称整个模型进程 RSS 上限。

交互 registry 的 I 为 run 全局共享，不是每轮 64；_used/checkpoint association 不得随无限 loop 线性增长。ID 由 run owner 按 turn token + 单调局部计数发行，不接受外部复用注册；仅保留活动请求和至多 I 个 checkpoint association，turn 排空后回收。submit 找不到、已过期或旧 turn 的响应返回 False，不能通过删除历史 set 后复用 ID。

completion/terminal/取消尾部在队列外保留，取消永不依赖往满队列 put sentinel；所有 channel 共用 I 尾部预算，每个 turn 最多一个 terminal。先排空该轮已接受事件，再交互尾部，最后其 terminal，sequence 按实际接受/尾部输出顺序继续。run completion 等所有活动轮封口，不因某轮 completed 提前结束。消费者不读时正常运行背压停住；cancel/aclose 仍须不依赖消费者而在测试 deadline 内回收全部任务与锁。

## 5. 审批必须驱动真实业务且安全默认拒绝

复用 `src/voidx/tooling/domain/interaction.py` 的具名响应/解析结果与 ports；旧 UserResponse 只在显式 legacy adapter 边界转换，不能丢失 resolution_reason 后重新自动批准。

| 输入 | semantic goal/loop 决策 | controller/store/start 行为 |
| --- | --- | --- |
| value 精确等于 approved，非 free_text，answered | approved | 仅当前未取消 owner 下提交 spec 并 start |
| value 精确等于 revised | revised | 不提交 INIT、不 start；返回修改路径供当前 intake 继续 |
| 允许的自由文本 | revised，保留反馈 | 不当作批准；修改后必须重新审批 |
| value 精确等于 cancelled | cancelled | 不提交、不 start |
| rejected / dismissed / timed_out | rejected；reason 分别为 user_rejected / dismissed / timed_out | 不提交、不 start |
| 无交互能力 | rejected + dismissed；该 reason 表示未获得交互回答，诊断另外标明能力缺失 | 不提交、不 start；不扩展既有 reason 枚举 |
| task_cancelled / owner 正在关闭 | rejected + task_cancelled | 永不 auto_approved；即使原请求此前返回批准，也在提交/start 前检查 owner 活性 |
| 未知 choice、跨 session/thread/turn、非法 scope | 验证错误 | pending 不被错误消耗，不推进业务 |

只允许旧 bootstrap 显式启用 legacy_auto_approve，且只覆盖旧契约中的无交互/超时/关闭提示场景；SDK 工厂始终禁用。取消整个 run 无论 legacy 标记如何均不得批准。不能用“合法 choice 就 approved”处理 revised/cancelled，也不能用 lower()/模糊字符串猜批准。

begin_cancel 同步封住 run 所有交互与 service.start admission，再取消生产者。审批提交与取消竞争以 owner admission 锁线性化：取消先取得则不产生 INIT/start；start 先取得且 durable 提交成功则取消流程必须 stop 该 generation，不能留下后台 pump。required 成功接受的请求恰有一个 resolved；未发布 required 的请求不产生孤立 resolved。

## 6. 所有权、锁与停止顺序

一个 run owner 统一拥有：根执行任务、mux、全部 turn supervisor、service pumps、dispatcher lease renewals、交互 Future/timer、各 child execution、ThreadStore/仓储实例、父子 session 锁和 workspace 锁能力。共享的宿主资源只借用，不由 SDK 实例关闭。不得通过临时 task 绕过 supervisor.spawn。

### 6.1 锁合同

根 session 锁先获取并重读；之后先登记 workspace ownership，再登记真实 child session。workspace OS 写锁延续运行时实际写入门槛，不在 intake 持有一个未使用的写锁跨审批等待；一旦写阶段获取则由 owner 持有至清理完成。相同 owner 的父子 acquire 复用一个 OS handle 并引用计数；release(child) 只释放借用，不解锁仍被其他 child/父使用的 handle。

逻辑重入不是允许并行写：同一 workspace 由 owner 内串行写 gate 调度不同 child；父等待 child 时不得持有该串行 gate。新 child 只能通过 owner capability 获取重入权限，另一个 SDK 实例/进程不因 workspace 相同获得重入资格。child session 锁按需获取、封口持久化后释放，取消时释放全部已登记项；禁止先持 child 再反向获取根锁。不得把根 HeadlessWorkspaceWriteLock 对象随意复制给每个 execution 后让任一 child 无条件 release。

### 6.2 唯一停止序列

1. 将 owner 置 STOPPING，拒绝新 stream/turn/task/interaction/start，同步 begin_cancel 关闭审批 admission。
2. 封住 scheduler 新 dispatch，调用既有 service.stop/deactivate 及 outbox 清理语义；取消并等待 pump、执行、renewal 和其他 producer。停调度与等待分开，不能拿 parent service 锁等待需要同一锁退出的 task。
3. 在所有生产者停止后处理真实 turn 失败/取消状态与持久化；保留 goal CAS、generation lease/fencing 约束。对已批准且已创建的 generation 必须写 durable 停止状态，不能仅 cancel Python task。
4. 清理交互 timer/Future，将已发布 required 的未交付 resolved 放入全局有界尾部；封口每个真实 turn，不向满业务队列发布 cleanup 事件。
5. 关闭本 run 的 child/root executions 和仓储资源；最后释放 child session locks、workspace OS handle、根 session lock，清空实例运行槽。共享实例按工厂 ownership 清单不关闭。
6. 完成独立 run completion；继续消费者可排空，aclose 不等待消费者排空。重复 cancel/aclose 幂等，不重写终态。

正常结束也执行对应 drain/stop/close，不调用会将已成功 generation 改为 cancelled 的 stop 分支。每个清理步骤都在 try/finally 中继续执行，不能用后一个 close 异常覆盖首错；内部错误按来源记录，返回 BaseExceptionGroup/ExceptionGroup 给显式关闭调用方。错误条目最多 P+T+I 加固定资源数，额外重复错误按来源计数和诊断日志汇总；公共事件只给脱敏摘要。即使清理失败也释放后续锁，并以 failed completion 而非 completed 收尾。

## 7. 持久化与恢复：沿用既有状态，不持久化流

已存在 `src/voidx/agent/application/automation/goal/recovery.py` 的 GoalRecovery 获取/续租 generation lease，校验协议序列和投影位置、投影 submitted records、为非终态补 wakeup；冲突/损坏不得降级新建任务。注意 GoalRuntimeCorruption 在该恢复器中会调用 fail_goal_generation 写 durable 失败而非继续向外抛出；SDK 接管方必须恢复后重读 lifecycle/decision，将其报告为失败，不能因 recover_generation 正常返回而启动新任务。复用 `src/voidx/agent/application/automation/goal/projector.py` 的业务投影，它与 UI semantic projector 不是同一职责。

恢复步骤固定为：根锁后重读 snapshot/workspace → 按 store 找该 parent 既有 generation → goal 走现有恢复及 lease/fencing 校验，loop 走现有 resume（原 session、非终态才恢复）→ 登记真实 child identities/锁/输出端口 → 恢复 runtime messages/state → 启动受管 pump。不能在输出/owner 尚未绑定时让 recovery pump 先跑。

| 故障断点 | 恢复及验收 |
| --- | --- |
| 审批尚未 durable 提交 | 不恢复 pending Future，不推断已批准；新 intake 重新审批 |
| durable INIT 已存在，start 返回前中断 | 校验同 generation/spec/parent，恢复已有 generation，不创建第二个 INIT/子 session |
| work/evaluator 协议 submitted 尚未 projected | GoalRecovery 线性投影，按既有 sequence/fencing 防重复；不重放 SDK 事件 |
| turn 文本已持久化，stream 未被消费 | 恢复消息与执行状态，不承诺补发旧 event_id/sequence |
| lease 被另一 owner 持有/过期或 journal 损坏 | 明确失败；不强抢、忽略冲突或改存储结构 |
| loop 等待时进程结束 | resume 保留原 session/iteration/due wakeup，不重复首轮；继续依赖已有 outbox claim |
| durable terminal 或用户 stop | 不复活；新的 stream 可进入同 profile 的新 intake |

新的 SDK run 使用新 run_id、新真实执行 turn_id 和新 sequence；持久化的 child session/thread/generation/attempt 身份保持不变。没有跨进程 pending interaction 恢复、SDK 事件日志或可靠 replay API。展示恢复继续使用既有 transcript snapshot 格式；存储/Schema 变更不属于本补充授权。

## 8. 分阶段 TDD 与文件门槛

以下所有新增测试文件标为 **planned**，必须先创建测试再执行命令，先确认因目标缺失 RED，再最小实现 GREEN，随后既有相关回归。仅导入失败/路径不存在不能算业务 RED。命令均从仓库根执行，通过 `./python.py test.py` 使用项目环境；每个测试以确定性模型替身、真实 runtime/controller/store/scheduler 和临时持久化为主，不依赖付费模型或外网，不用 fake graph/NullUI 替换生产编排。

### S1：exact 审批决策（下一次需批准的最小切片）

- 先扩展已存在 `src/tests/test_sdk/test_interaction_coordinator.py`：goal/loop 的 approved/revised/cancelled、free_text、超时、dismiss、取消竞争；检查精确 decision/reason 与不消耗非法响应。
- 最小修改已有 interaction_coordinator 的决策映射，不接自治工厂、不切默认、不修改 UI Schema 或旧工具路径。
- 验收：只有 exact approved 获批，task_cancelled 从不批准，checkpoint/permission 回归保持。

```bash
./python.py test.py --backend -- src/tests/test_sdk/test_interaction_coordinator.py src/tests/test_sdk/test_interaction_contracts.py
```

### S2：运行级有界基础设施

新增 `src/tests/test_sdk/test_run_event_mux.py`（planned）、`src/tests/test_sdk/test_run_ownership.py`（planned）。实施 planned run_event_mux/run_lifecycle，并最小扩展 channel/supervisor/coordinator：T/Q/P/I 总预算、跨轮公平、唯一终态、独立 completion、有限注册表与受管 pump 入口。

```bash
./python.py test.py --backend -- src/tests/test_sdk/test_run_event_mux.py src/tests/test_sdk/test_run_ownership.py src/tests/test_sdk/test_semantic_channel.py src/tests/test_sdk/test_run_supervisor.py
```

RED/GREEN 场景必须包含 Q=1、两个真实 identity 交错、一个热生产者不饿死其他轮、连续上千短轮无历史列表增长、全局 I 而非每轮 I、封口后发布拒绝、pump 异常唤醒空队列、满队列 cancel/aclose 在 5 秒 deadline 内完成，遗留受管 Task/Future=0。计数器验证预算，不能只用偶然 RSS 样本证明上界。

### S3：会话路由与无头生产装配

新增 `src/tests/test_sdk/test_autonomous_session_profiles.py`（planned）、`src/tests/test_sdk/test_autonomous_session_execution.py`（planned）。实施 planned autonomous_session/autonomous_headless；改已有 headless、SDK、pump/runtime child 输出绑定与 goal/loop 交互适配。既有工具安全默认拒绝与 legacy 显式兼容必须在同一切片测试。

```bash
./python.py test.py --backend -- src/tests/test_sdk/test_autonomous_session_profiles.py src/tests/test_sdk/test_autonomous_session_execution.py src/tests/test_application/test_goal_idle_turn.py src/tests/test_application/test_loop_idle_turn.py src/tests/test_agent/adapters/tools/test_goal_tool.py src/tests/test_agent/adapters/tools/test_loop.py
```

验收不能止于 prompt/profile 断言：

- 无 session 创建 coding；固定 snapshot 优先；已有 goal/loop 路由；未知/不可用/损坏 profile 拒绝且锁释放。
- goal：真实 init 工具 required → SDK submit approved → 真 GoalIntakeController 提交 → ThreadStore INIT → GoalService.start → work turn → checkpoint → GoalEvaluator turn → durable terminal → stream 结束；至少一次未达标再工作，看到多个真实 turn/session。
- loop：真实 init/controller/start → 第一 iteration → durable decision/outbox 等待 → 第二 iteration → 自然终止或 SDK cancel；确认新 turn_id、原 loop session、停止后无 wakeup 被执行。
- revised/cancelled/timeout/no interaction 不产生 INIT/start；批准与取消竞争不遗留 generation/pump。
- 工具输出、评估文本、状态、等待、HITL 均带真实 child identity；使用真实本地文件工具验证结果，不只 mock start 被调用。

### S4：恢复、锁与故障收敛

新增 `src/tests/test_sdk/test_autonomous_session_recovery.py`（planned）、`src/tests/test_sdk/test_autonomous_session_locks.py`（planned）。修改已有 headless locks、工厂所有权和 service 接管边界，不修改存储 schema。

```bash
./python.py test.py --backend -- src/tests/test_sdk/test_autonomous_session_recovery.py src/tests/test_sdk/test_autonomous_session_locks.py src/tests/test_sdk/test_headless_resources.py src/tests/test_goal src/tests/test_loop src/tests/test_persistence/test_goal_generation_storage.py
```

覆盖 §7 所有断点；父等待 child 的写锁重入不死锁，两个 child 不并行写，同实例二次 stream 拒绝，其他实例/进程不能重入；init/persist/stop/close 多处同时失败仍全部释放且错误不被覆盖。关闭后重开真实 store 验证 durable 终态、原 child session 和去重，不以内存 mock 代替 recovery 验收。

### S5：SDK → Gateway/client 多真实 turn 投影与恢复

新增 `src/tests/test_sdk/test_autonomous_gateway_projection.py`（planned）、`src/tests/test_sdk/test_autonomous_gateway_browser.py`（planned）。最小扩展已有 core/projector/runtime 输出；本阶段保持 legacy 默认。

```bash
./python.py test.py --backend -- src/tests/test_sdk/test_autonomous_gateway_projection.py src/tests/test_sdk/test_gateway_integration.py src/tests/test_presentation/adapters src/tests/test_presentation/gateway
PLAYWRIGHT_BROWSERS_PATH="$PWD/.voidx/browser-cache" ./python.py test.py --backend -- src/tests/test_sdk/test_autonomous_gateway_browser.py src/tests/test_sdk/test_gateway_browser.py
```

通过真实 SDK stream 驱动 Gateway/client，覆盖 root intake、work/evaluator、连续 loop、跨 child HITL 所有权、唯一终态、工具结果、取消和 reconnect/transcript restore。浏览器看到正确子身份和完整结果；不能只喂伪造事件或只测试 SDK 直接消费者。保持既有 UI protocol Schema 等价；恢复不能把多个 turn 合并成根文本。未通过则 legacy 保持，不能以部分 coding 测试替代。

### S6：生产入口迁移与主规范 Stage D（后续独立批准 gate）

只有 S1–S5 对最终集成树通过，才另行批准 `session.submit` 生产链路迁移；不是 SDK 测试通过自动启用。必须验收既有 `src/voidx/presentation/gateway/session/method/sessions.py` 的实际入口和相关客户端，不只显式 semantic 测试工厂。

Stage D 还要求核心 UI 控制调用全部迁移、真实 child 输出无 legacy 偷渡、架构依赖检查及前端/桌面/浏览器完整回归通过；未覆盖族保留旧路由。此次不删除 UI ports、不默认切换、不归档主规范或本补充。

## 9. 验证命令与证据约束

聚焦阶段 GREEN 后再扩大：

```bash
./python.py test.py --backend -- src/tests/test_sdk src/tests/test_goal src/tests/test_loop src/tests/test_architecture
./python.py test.py --backend
./python.py test.py --frontend
./python.py test.py --desktop
./python.py test.py
```

`test.py` 非 verbose 默认输出 nested JSON：顶层 `results` 数组及 `exit_code`，每个 suite 有自己的结果对象；自动化必须解析 JSON，验证整体与每个 suite 状态/计数，不能 grep passed、把嵌套计数当顶层字段，或只看 shell 退出码。浏览器需单独使用上述 PLAYWRIGHT_BROWSERS_PATH 命令并报告实际执行/skip，不能把缺浏览器跳过视为通过。不杜撰不存在的全量脚本或 --check 参数。

用户提供历史基线：backend 6650、frontend 1019、desktop 27、32 skips，以及 21 browser；**本次未重跑，不能证明自治会话能力，也不能证明当前脏树全绿**。已知 TypeScript 既有失败及历史 frontend timeout 尚未在本任务解决；不得声称前端/Stage D 已通过。未来报告须记录命令、cwd、代码状态、exit_code、各 suite counts/skip、超时和失败明细；变更后重跑受影响检查。

## 10. 文档质量门槛与实施授权

### Fresh-reader 检查

读者无需父会话即可知道：模式来自 session；stream 生命周期覆盖整个任务而非永久会话；每轮真实身份保留；审批后执行到 durable 终态；多轮完成不伪造 turn；legacy 默认暂不变。主规范单轮条款被扩展的准确范围在 §1 指明，现状与 planned 路径在 §2/§3/§8 分开。

### Execution-readiness 检查

实施顺序、文件归属、真实调用链、精确审批表、Q/T/P/I 内存预算与公平性、owner 停机顺序、父子锁重入、recovery 断点、失败传播和阶段命令均已明确。不存在以“TODO 另定架构”替代本次批准设计的事项。新增路径须先创建相应测试，不将当前不存在的测试当成可立即通过的检查。后续生产迁移/Stage D 是明确验收及批准门槛，不是当前设计的未决架构。

**下一次最小批准请求仅为 S1：已有 coordinator 的 goal/loop exact 决策测试与修复。** 本次交付是文档，不启动 S1，不声称任何 planned 文件已实现或自治验收已通过。
