# Goal 会话模式：退出后续聊与再初始化

> **Status: Done** — Archived on 2026-08-04.

## 来源

2026-08-03 brainstorm 结论。属于 agent 五层重构（turn / tool / prompt / loop / goal）中 **goal 层**的落地第一步；tool 层本期内先用现有 `ToolPolicy` 扩展（phase 机制），不起新抽象。

## 背景与问题

goal 模式的会话今天是一个"死房间"：

- 首条消息经 `GoalIntakeService` 在脱离主会话的侧线程（`goal-intake:<parent>`）跑 intake，spec 确认后启动 `goal:<parent>:<run_id>` 侧线程跑 work/evaluator 循环。
- goal 结束后，`_route_autonomous_followup` 只打印一句提示；`message_count > 0` 后首消息路由已失效，用户除了 `/coding` 切走什么也做不了。
- goal 完成/停止只更新侧线程状态与 UI 事件，**主会话没有任何结果记录**。

目标语义（已与用户确认）：

1. goal 模式不变，goal 退出（finished / blocked / stopped）后可继续对话；
2. 对话落在主会话，不在 `GoalIntakeController` 里塞闲聊逻辑（该 controller 保持单一职责：spec 收集 + cancel）；
3. 退出后可作为普通 turn 对话，也可再次 `goal(op=init)` 进入新的 while-condition 循环；
4. goal 的 work 阶段本质是一轮 coding turn，只是去掉交互类工具（checkpoint、clarify 等）；evaluator 是只读判定 turn。这一认知用于后续 work 阶段与 coding profile 的归并，本期不做。

## 设计

### 状态机

```
idle ──goal(op=init) + 用户批准──▶ running ──finished / blocked / stopped──▶ idle
 ▲                                                                            │
 └──────────── 主会话注入结果摘要，继续对话或再次 init ◀───────────────────────┘
```

- **idle**：主会话内跑普通 turn。profile = goal，工具 = 只读集（read / find / search / lsp / document）+ clarify + goal。提示词在现有 intake 硬规则基础上放宽：允许正常对话与回答问题，但**不做任务执行**（不写代码、不跑命令）——goal 模式的承诺是工作在自治循环里做。用户提出目标时，turn 内调用 `goal(op=init)` 走审批，批准即进入 running。对话历史落在主会话，天然连续。
- **running**：维持现状——侧线程跑 work + evaluator，主会话消息走 guidance 队列。
- **退出后**：向主会话注入一条结果摘要（现状缺失），回到 idle。

### 变更点

| 文件 | 变更 |
|---|---|
| `src/voidx/agent/domain/goal.py` | `GoalToolView` 新增 phase `"idle"`：只读集 + `clarify` + `goal`，去掉 websearch/webfetch/mcp/skill；`GOAL_INTAKE_DIRECTIVE` 放宽为 idle 对话指令（保留"不执行任务、spec 只走 goal(op=init)"硬规则） |
| `src/voidx/agent/domain/prompt_policy.py` | 新增 `GoalPromptPolicy`：goal 模式会话 turn 的提示词策略（idle 指令 + 抑制 coding 专有 section 中与自治运行冲突的部分） |
| `src/voidx/agent/domain/profile.py` | goal profile 描述补充 `prompt_policy=GoalPromptPolicy()`（或在使用处装配，按现有 chat 模式先例） |
| `src/voidx/agent/application/agent_service.py` | `_route_autonomous_followup`：goal profile 且无 active goal 时，把消息路由为主会话 goal-profile turn（复用 `CodingService.run_turn`，context 带 `runtime_profile=GOAL_PROFILE` + `GoalToolView(phase="idle")` + `goal_intake_controller`）；`_handle_goal_first_message` 不再起 `goal-intake:` 侧线程，统一走会话 turn 路径 |
| `src/voidx/agent/application/goal_service.py` | goal 到达终态（finished / blocked / cancelled）后，向父会话注入结果摘要消息（objective、outcome、evaluator summary、attempts），使后续对话有上下文 |
| `src/voidx/agent/application/goal_intake.py` | `GoalIntakeService` 的脱离线程 intake 路径废弃，删除或收敛为仅供会话 turn 复用的 spec 提交辅助 |

### 不变的部分

- work / evaluator 侧线程循环、durable outbox、wakeup pump、guidance 队列：`src/voidx/agent/goal/{runner,scheduler,evaluator,controller}.py` 全部不动。
- `/goal <objective> --accept <condition>` 快捷路径：直达 `GoalService.start`，不进 idle turn。
- loop 模式本期不动；同款的"会话前门"模式后续单独 spec。
- `GoalIntakeController` 保持 spec 收集 + cancel 单一职责，在 idle turn 内继续使用。

### 风险与权衡

- **stable-prefix 缓存**：`runtime_context.py` 的 ContextCompiler 按指纹复用 SystemMessage 前缀。`GoalPromptPolicy` 的 section 必须保持与 coding 相同的排序骨架（Base System 在前），否则 goal 会话的缓存全失效。
- **重启恢复**：goal 终态注入主会话摘要依赖 `GoalService` 观察到终态。终态判定当前在 `_status()` 惰性发生，进程重启后无人轮询时摘要可能缺失——接受此限制：摘要是"尽力而为"的会话便利，不替代 `/goal status`。
- **旧 `goal-intake:` 线程**：一次性 throwaway，无持久化依赖，无迁移问题。

## 验证

- `./test.py --backend -- src/tests/test_application -k "goal"`：goal 服务/路由相关用例全绿。
- 新增用例：
  - goal 会话 idle turn 可调 `goal(op=init)`，批准后 `GoalService.start` 被调用；
  - goal 会话 idle turn 无写工具（`GoalToolView(phase="idle")` 的 `visible_tool_ids` 断言）；
  - goal finished 后主会话出现结果摘要消息；
  - goal finished 后续消息走 idle turn 而非提示语；
  - goal running 中主会话消息仍走 guidance 队列（现有行为不回归）。

## 后续（不在本期）

- prompt 组合式装配（身份/风格/职责边界/工作流/规则的 section 件），替换"以 coding 为底版打补丁"的 `PromptPolicy` 覆盖模式；
- 三个 decision controller（`GoalController` / `LoopAttemptController` / `GoalIntakeController`）收敛为一个 lifecycle-decision 原语；
- goal work 阶段与 coding profile 归并（work = coding turn 减交互工具）；
- loop 模式的会话前门。
