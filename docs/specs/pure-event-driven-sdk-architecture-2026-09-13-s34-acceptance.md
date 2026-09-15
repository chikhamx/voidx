# 无头自治 SDK：S3/S4 验收记录

- 日期：2026-09-15；读者：human+llm。
- 结论：**S3 真实无头自治装配、S4 恢复/锁/故障收敛，通过本记录所列场景验收。**
- 范围依据：[自治会话补充规范](pure-event-driven-sdk-architecture-2026-09-13-autonomous-session.md) §8 S3/S4，连同其引用的身份、预算、审批、停止和恢复合同。
- 不代表[主规范](pure-event-driven-sdk-architecture-2026-09-13.md)全部完成。S5 Gateway/client 多真实子轮投影、生产 `session.submit` 迁移、frontend/desktop 和 Stage D 仍是独立门槛；两份规范均不归档。
- 审查方式：父流程直接检查实现、测试断言和实际运行结果。仅返回 `[completed]` 且无报告的独立审查没有计入证据。

## 验收对照

以下测试路径均相对 `src/tests/test_sdk/`。它们在最终 backend 集成运行中执行；场景通过不意味着对所有可能调度交错的形式化证明。

| 合同 | 实现位置（相对 `src/voidx/`） | 主要验收文件及观察 |
| --- | --- | --- |
| 固定 profile 路由，不猜 prompt | `bootstrap/headless.py`、`bootstrap/autonomous_headless.py` | `test_autonomous_session_profiles.py`：coding 默认、snapshot 优先和 registry fallback；`test_autonomous_recovery_identity.py`：child snapshot 与身份冲突 |
| 审批真正控制提交与启动 | `agent/application/runtime/interaction_coordinator.py`、`agent/adapters/tools/automation/{goal,loop}.py` | `test_autonomous_approval_rejections.py`：goal/loop × revised、cancelled、真实超时、缺 requester、自由文本；重开 store 查无 INIT/generation/outbox/child，无 start/pump，修改意见进入真实 ToolMessage。`test_autonomous_init_interactions.py` 补充 exact 决策和能力边界 |
| goal 多轮自然完成 | `bootstrap/autonomous_headless.py`、既有 goal service/scheduler/evaluator | `test_autonomous_session_execution.py::test_sdk_goal_approval_two_attempts_natural_completion`：真实审批、work/checkpoint/evaluator，再次 work/evaluator，五个执行身份、两次 attempt、重开 store 为 completed |
| loop 多轮持续至取消 | 同上及既有 loop service/scheduler/controller | `test_autonomous_session_execution.py::test_sdk_existing_loop_approval_two_iterations_complete`：真实两轮、稳定 child session、不同 turn、durable cancelled 和无 pending wakeup；`test_autonomous_status.py` 覆盖 guardrail 产生合法 needs_user |
| 工具、评估、状态、HITL 保留真实 child 身份 | `agent/adapters/langgraph/runtime/{semantic_output,permission_flow,llm_turn}.py`、`agent/application/runtime/scheduler_events.py` | `test_autonomous_session_tools.py`：真实文件写读和 evaluator 检查、权限交互、取消；`test_autonomous_status.py`：持久化 available_at、status.finished 在 turn terminal 前、pre-turn 故障不造身份 |
| run completion 独立且事件驱动 | `agent/application/runtime/{run_ownership,run_event_mux,scheduler_events}.py` | `test_goal_terminal_notification.py`：真实工作暂停期间无周期 status 读取；sticky post-commit 通知不丢、不越过背压状态输出；提交失败不通知成功。`test_autonomous_terminal_consistency.py`：completion 等清理，blocked 不被 stop 改写 |
| 共享预算、背压和唯一终态 | `agent/application/runtime/{run_ownership,run_event_mux,semantic_channel}.py` | `test_run_event_mux.py`、`test_run_ownership.py`、`test_interaction_coordinator.py`：Q=1、公平消费、上千轮回收、T/P admission、renewal 预留、交互共享预算、满队列取消 |
| 取消清理与异常边界 | `sdk/agent.py`、`bootstrap/autonomous_headless.py`、`agent/application/runtime/{run_supervisor,run_ownership}.py` | `test_autonomous_session_cancellation.py`：goal/loop 审批中、活动轮、等待、暂停消费、满队列；`test_owned_cleanup_errors.py`：真实 child 多错、继续释放、显式关闭保留原错聚合，普通 stream 仅脱敏 Run failed；`test_headless_resources.py` 补充初始化和关闭故障 |
| INIT 中断、协议重投影和消息恢复 | `agent/application/automation/goal/{goal_service,recovery,projector}.py`、`agent/adapters/persistence/thread_repository.py` | `test_autonomous_session_recovery.py`：独立 durable INIT、submitted checkpoint、corruption；`test_autonomous_goal_recovery_crash.py`：真实 evaluator 提交后进程退出，恢复不重审批、不重执行已提交 evaluator、保留 generation/child |
| loop 原 outbox/iteration 恢复 | `agent/application/automation/loop/loop_service.py`、既有 dispatcher/store | `test_autonomous_loop_recovery.py`：初始 loop_prompt 与真实 iteration 进程中断，保留 session、available_at、iteration 和历史消息，不重复首轮 |
| lease/fencing 与拒绝接管外部任务 | `agent/adapters/persistence/thread_repository.py`、goal recovery/service | `test_autonomous_goal_recovery_crash.py`：lease 到期、合法新 owner、投影/摘要/handoff 边界抢占，旧 SDK 不 register/pump/stop 外部 generation；`test_autonomous_recovery_identity.py`：多活动 generation、孤立 INIT 冲突、child 元数据不一致先失败 |
| 终态不复活 | `bootstrap/autonomous_headless.py`、goal/loop service | `test_autonomous_recovery_identity.py`、`test_autonomous_session_recovery.py`：启动前已终态可新 intake、旧状态不变；`test_goal_terminal_notification.py`：本次恢复投影后刚终结则直接结束、零新模型调用 |
| 父子锁、串行写、跨进程隔离 | `agent/adapters/persistence/headless_locks.py` | `test_autonomous_session_locks.py`：同 owner 共享 handle、两个 child 串行写、父等待不持 gate、不同 owner/进程不可重入、取消等待/初始化失败释放；真实 SDK 文件及多错测试补充运行栈验证 |

## 本轮补齐的缺口

最后新增十项真实 SDK 拒绝审批用例。首个有效业务 RED 为 **9 passed / 1 failed**：goal 自由文本意见被工具返回丢弃，loop 对照正常。最小修改 `agent/adapters/tools/automation/goal.py` 保留 `revise:` 后的反馈，不改变审批结果和提交逻辑。新矩阵连同 init 交互、goal/loop 工具回归为 **60 passed**。

此前已闭合的整改包括：child 关闭错误有界聚合、自动 stream 收尾脱敏、真实状态时间与终态输出、goal 完成通知替代 SDK 状态轮询。既有 outbox pump 调度逻辑没有在此阶段重写。

## 最终运行证据

工作目录：仓库根 `/Users/chikham/workspace/voidx`；macOS arm64，项目 `./python.py` 环境。测试 timeout 均为 600 秒。结果针对脏工作树，不用 HEAD 代替实现版本；源码 SHA-256 清单和日志哈希保存在 `/tmp/voidx-sdk-s34-final-evidence.json`。

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.voidx/browser-cache"
export PYTHONSAFEPATH=1
export PYTHONPATH="$PWD/src"
./python.py test.py --backend
./python.py test.py --backend -v -- src/tests/test_sdk/test_gateway_browser.py
git diff --check
```

| 运行 | 实际结果 | 本地日志 |
| --- | --- | --- |
| 全 backend | PASS，6939 passed、0 failed、32 skipped、5 warnings；nested exit_code=0 | `/tmp/voidx-sdk-s34-final-backend.json` |
| 既有真实浏览器兼容回归 | PASS，21 passed、0 failed、0 skipped；nested exit_code=0 | `/tmp/voidx-sdk-s34-final-browser.json` |
| 最后审批反馈 RED | FAIL，9 passed、1 failed；nested exit_code=1 | `/tmp/voidx-approval-feedback-parent-red.json` |
| 审批与工具聚焦 GREEN | PASS，60 passed；nested exit_code=0 | `/tmp/voidx-approval-feedback-parent-green.json` |
| 空白/补丁检查 | `git diff --check` 通过 | 最终证据清单 |

必须解析 `results[].status/exit_code` 及非零测试计数，不能把 runner 的外层退出码 0 当作通过。32 skips 和 5 warnings 保留为限制，不算已测试场景；单独浏览器的 21 项实际执行无 skip，但不是 S5 自治多子轮浏览器验收。`/tmp` 是本地临时证据，迁移机器或清理后需按命令重新生成，不把链接当永久 CI 产物。

## 后续独立门槛与不变量

1. S5：真实 SDK → Gateway/client 多 child session/thread/turn 投影与恢复，不得将子轮改写为根身份。
2. 生产 `session.submit` 迁移：保留既有消费链路和兼容测试，不能仅因 backend 通过就切默认。
3. frontend、desktop、Stage D：分别执行其要求的编译、测试、迁移及最终验收，再判断主规范归档条件。

后续实施继续保持：不新增公开 profile 参数或后台任务 API；不改 UI 协议/存储 schema；不使用 fake graph/NullUI 替代生产编排；保留用户脏树。JSONL 与 SQLite 不构成跨介质 crash-atomic 事务，恢复摘要沿用既有 marker 重试去重；不存在跨进程 pending interaction 恢复或 SDK event replay 承诺。
