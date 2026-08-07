---
name: goal-phase-transparent-resume
display_name: Goal 阶段检查点与透明续跑
description: 未 commit 的 work/evaluator 阶段停在该阶段并可自动续跑；生命周期对用户完全透明，不暴露 start/stop/continue
doc_type: tech-design
audience: human+llm
status: draft
date: 2026-08-06
---

# Goal 阶段检查点与透明续跑

## 1. 结论

将 goal 的 work / evaluator 从「一次 attempt 整段原子 commit」改为「阶段检查点 + 未完成可恢复」。任一阶段在 durable commit 之前中断，都停在该阶段（`needs_resume`），并在用户下一条消息时**自动**从该阶段重入。

生命周期对用户完全透明：

- 不新增 `/goal continue`
- 不把 `/goal start|stop|status|continue` 作为用户心智或主提示
- 用户只描述目标、正常对话；系统记住停在哪并自动续

**不做兼容层**：删除或收敛依赖旧语义的路径（整段 attempt 原子失败即 blocked/终态、host 在可恢复状态下只 guidance / 教用户 status|stop、`missing_goal_decision` 直接 blocked 终态等），以本设计为唯一规范。

## 2. 问题

### 2.1 现象

1. work 或 evaluator 在 decision/阶段结果未 commit 时因 LLM 退出、进程崩溃、lease 丢失等中断。
2. host 会话只能 `guidance queued`，或 stop 后进 idle「继续」却不执行。
3. 重新启动 goal 会 `new_generation()` + 新 session，旧 work 上下文无法接上。
4. UI/文案引导用户使用 `/goal status`、`/goal stop`，把内部调度暴露成操作手册。

### 2.2 根因

| 层 | 现状 | 后果 |
|----|------|------|
| runner | work → evaluator → **一次** commit | 中段失败无法表达「停在 evaluator」 |
| recovery | side effect 后未 commit → `needs_user`，但 goal 无按 phase 重入 | 状态可脏可停，不能续 |
| host 路由 | active → 只 guidance；非 active → idle | 可恢复 run 被当成无任务或死房间 |
| UX | 提示 status/stop | 用户被要求操作生命周期 |
| 重启/替换 | start 总是新 generation | 续跑被实现成另起炉灶 |

### 2.3 非目标（本期不做）

- 不把 evaluator 改回加载 work 全量会话历史（detached + evidence 注入保持）。
- 不把 loop 一并改成同样透明模型（可后续对齐）。
- 不做「已 `cancelled` 终态的 undo」。
- 不引入用户可见的 phase 命令语言。

## 3. 用户可见契约

用户在 goal 模式下只需要：

1. **提出目标**（自然语言或现有 init 审批流）
2. **运行中补充说明**（作为 guidance）
3. **中断后继续说话**（任意相关消息，含「继续」）→ 系统自动从当前未完成阶段恢复
4. **明确取消** → 走通用中断/取消（Esc、stop generation、或明确「停下来」的取消语义），不是 `/goal stop` 教学

系统向用户展示的是自然语言进度，例如：

- 「目标进行中（实现阶段）…」
- 「验收阶段中断，已根据你的消息从验收继续…」
- 「目标已完成 / 已阻塞：…」

**禁止**主路径文案出现：`Use /goal status or /goal stop`、`/goal continue`、教用户 start/stop/continue。

### 3.1 `/goal` 表面收敛

| 输入 | 行为 |
|------|------|
| `/goal` | 仅切换到 goal profile |
| `/goal <objective> --accept <cond>` | 可选快捷启动（内部 API），不作为生命周期模型宣传 |
| `/goal status\|stop\|start\|continue` | **删除**（不做兼容别名） |

内部 `GoalService` 仍可有 `start` / `stop` / `resume_phase` 等方法，供 runtime 与路由调用，**不**映射为用户 slash 生命周期。

## 4. 内部状态机

```text
idle
  │  goal(op=init) 用户批准 或 快捷 start
  ▼
work          phase=work, phase_status=running
  │  work turn 成功 → checkpoint commit
  ▼
evaluator     phase=evaluator, phase_status=running
  │  goal(op=decision) durable commit
  ├─ finished  → idle + 主会话结果摘要
  ├─ continue  → 下一 attempt，phase=work
  └─ blocked   → idle + 阻塞摘要（可再 init）

运行中任意 phase：
  未完成 durable 边界 commit 的中断
    → phase 不变
    → phase_status = needs_resume
    → lifecycle = needs_user（可恢复，非终态）
    → 用户下一条 host 消息：guidance（可选）+ 自动 resume 该 phase
```

### 4.1 阶段与 commit 边界

| 边界 | 何时 commit | 落盘内容 |
|------|-------------|----------|
| A. work 完成 | work turn 正常返回后、启动 evaluator **前** | `current_phase=evaluator`，`last_work_evidence`，`phase_status=running`，attempt_count **不**递增 |
| B. attempt 完成 | evaluator 提交 decision 且 store commit 成功 | decision 结果；`attempt_count` 递增；continue 则回到 work 或 finished/blocked 终态语义 |
| 中断 | 任一边界前失败 | `phase_status=needs_resume`，`interrupt_reason`，**不** new generation |

**attempt_count** 只在边界 B 成功后变化。因此「evaluator 跑了但未 decision commit」时 status 可仍显示当前 attempt 进度，而不是假完成。

### 4.2 `missing_goal_decision` 新语义

旧：evaluator 未调用 `goal(op=decision)` → `blocked` + `missing_goal_decision`（易变终态/不可续）。

新：→ `needs_resume` @ `evaluator`，`interrupt_reason=missing_goal_decision`。  
用户再发消息 → 只重跑 evaluator（使用已落盘的 `last_work_evidence`）。

仅当连续 resume 超过策略上限（见风险）或用户取消时，才升为 `blocked`/`cancelled`。

### 4.3 GoalState 字段（目标形状）

在 `GoalState`（`src/voidx/agent/domain/automation/goal.py`）增加：

```text
current_phase: Literal["work", "evaluator"] = "work"
phase_status: Literal["running", "needs_resume", "committed"] = "running"
last_work_evidence: dict | None = None
  # 建议键：assistant_summary, tool_result_summaries, work_turn_id?, finished_at?
interrupt_reason: str = ""
```

现有 `attempt_count` / `max_attempts` / evaluator 摘要字段保留；`active` 与 lifecycle 的关系以 thread lifecycle 为准，避免双源真相。

`phase_status=committed` 仅作边界瞬间或内部标记；对外可恢复视图以 `running | needs_resume` 为主。

## 5. 恢复策略

| 中断点 | `current_phase` | 恢复动作 |
|--------|-----------------|----------|
| work turn 未完成 | work | 同 `goal:{parent}:{generation}` session 再跑 work（会话历史保留） |
| work 已边界 A commit，evaluator 未完成 | evaluator | **只**跑 evaluator，注入 `last_work_evidence`；不重跑 work |
| evaluator 已有 decision，store commit 失败 | evaluator | 优先重放 commit；不能则 needs_resume 再评 |
| finished / blocked / cancelled | — | 不自动 resume；idle 对话或新 init |

**硬约束：**

- resume **禁止** `new_generation()` 与新 goal session id
- resume **禁止**丢弃未终态 thread 的 work session 绑定
- evaluator 保持 `detached=True`，证据只来自 checkpoint + 当次只读工具

## 6. Host 路由（透明续跑）

改写 `AgentService._route_autonomous_followup`（goal 分支）为：

```text
status = goal_service.status(parent)  # 含 needs_resume 的可恢复状态

if status is resumable (phase_status == needs_resume 或 lifecycle == needs_user 且同 generation 可恢复):
    if user_input.strip():
        submit_guidance(user_input)   # 可选；空则纯 resume
    await goal_service.resume_phase(parent)   # 内部 API
    提示自然语言：从哪一阶段继续
    return

if status is actively running (phase_status == running 且非 needs_user 等待):
    submit_guidance(user_input)
    简短确认已记录补充说明（不提 slash）
    return

# 无活跃/可恢复 goal
idle turn（可 goal(op=init)）
```

要点：

- 「继续」**不是**特殊命令，只是普通用户消息触发 resume。
- 可恢复时 **不得**落入 idle-only 对话却不推进 phase。
- running 时消息仍是 guidance，不抢占当前 phase 的 runner（除非产品后续做协作式取消，本期不做）。

### 6.1 取消

| 来源 | 结果 |
|------|------|
| 通用 stop generation / Esc 等运行时取消 | 当前 phase → `needs_resume` 或明确 cancel 策略二选一（见下） |
| 用户明确放弃目标 | `cancelled` 终态 → idle + 摘要 |
| 内部 `GoalService.stop` | 仅供系统；无用户 slash |

**推荐默认：** 对「生成中断」偏 `needs_resume`（可续）；对「用户明确取消目标」才 `cancelled`。  
若现有 stop generation 无法区分，优先 `needs_resume`，避免再出现「一断即死」。

## 7. 组件职责

| 组件 | 路径 | 职责变化 |
|------|------|----------|
| GoalState / GoalSpec | `domain/automation/goal.py` | 阶段字段；evidence 结构 |
| GoalRuntimeRunner | `application/automation/goal/runner.py` | 按 `current_phase` 进入 work-only 或 evaluator-only；边界 A/B commit |
| GoalEvaluator | `application/automation/goal/evaluator.py` | 仍 detached；evidence 来自 checkpoint（不仅当次 work_result 内存） |
| GoalService | `application/automation/goal/goal_service.py` | `resume_phase`；status 暴露 phase；start 仅新目标；删除对用户 slash 的 status/stop 依赖 |
| GoalRuntimeScheduler | `application/automation/goal/scheduler.py` | 支持按 phase 入队 payload（phase + goal_state） |
| RuntimeDispatcher / Recovery | `application/runtime/dispatcher.py`, `recovery.py` | 未 commit → needs_user/needs_resume；与 phase 字段对齐 |
| AgentService 路由 | `application/agent_service.py` | 可恢复则自动 resume；去掉 status/stop 教学文案 |
| Slash `/goal` | `slash/commands/mode.py`, `registry.py` | 仅 profile 切换 + 可选快捷 init；**删除** status/stop/start/continue 子命令 |
| 测试 | `tests/test_goal/*`, `test_slash/test_slash_goal.py`, routing 测试 | 锁定阶段恢复与透明路由；删除旧 slash 生命周期用例 |

## 8. 数据流（resume evaluator 示例）

```text
1. work 完成 → store commit 边界 A
   context.goal_run = {
     current_phase: "evaluator",
     phase_status: "running",
     last_work_evidence: {...},
     attempt_count: N
   }

2. evaluator turn 第 1 次 LLM 后进程退出
   → recovery/dispatcher: phase_status=needs_resume,
     interrupt_reason=..., lifecycle=needs_user
   → attempt_count 仍为 N（边界 B 未过）

3. 用户在 host 发送「继续」或任意补充
   → route: guidance + resume_phase(parent)
   → enqueue outbox: phase=evaluator, 带 checkpoint state
   → GoalEvaluator.run_phase(evidence=last_work_evidence)
   → decision commit 边界 B
```

## 9. 与既有设计的关系

| 文档 | 关系 |
|------|------|
| `docs/archive/goal-conversational-mode-2026-08-03.md` | idle / running / 退出后主会话摘要仍然成立；**修正** running 中断后只能 guidance/stop 的隐含假设 |
| `docs/archive/goal-evaluator-loop.md` | 工具化验收、durable wakeup 保留；**修正**「整段 attempt 一次决策」为阶段检查点 |
| `docs/design/goal-evaluator-context-pollution.md` | evaluator detached 与不污染 host **保持**；续跑不得回退该修复 |

冲突时以本文为准。

## 10. 风险与策略

| 风险 | 缓解 |
|------|------|
| work 续跑重复 side effect | 同 session 历史 + 目标提示强调基于仓库现状；不重置 workspace |
| 「继续」vs「换新目标」 | 默认可恢复则 resume；用户明确新目标时 idle/`goal(op=init)` 替换（新 generation 仅此时允许） |
| 自动 resume 死循环 | `needs_resume` 连续失败计数；超限 → blocked + 主会话说明 |
| store 形状变化 | GoalState 新字段一次到位；无旧字段兼容分支，测试与本地状态按新模型 |
| slash 删除破坏脚本 | 明确 breaking；脚本改走 profile + 自然语言/ init |

## 11. 验收标准

1. **evaluator 中断可续**：仅 1 次 LLM 后崩溃 → `current_phase=evaluator`, `phase_status=needs_resume`；用户只发「继续」→ 只重跑 evaluator，**同 generation / 同 goal session**。
2. **work 中断可续**：work 中崩溃 → 同 work session 恢复，不新建 generation。
3. **边界 A 存在**：work 成功后、evaluator 前崩溃 → 不重做 work，evidence 来自 checkpoint。
4. **透明 UX**：主路径无 start/stop/continue/status 教学；`/goal status|stop|continue` 不存在。
5. **idle 边界**：仅终态或无 run 时「继续」才走 idle；可恢复时不得只闲聊不推进。
6. **detached 不回退**：evaluator 不写 host session、不加载 host 历史。
7. **测试**：阶段 checkpoint、resume_phase 路由、`missing_goal_decision`→needs_resume、slash 收敛相关用例全绿。

## 12. 建议实现顺序

1. GoalState 字段 + store 读写  
2. runner 按 phase 拆分 + 边界 A commit  
3. `missing_goal_decision` / dispatcher 中断 → needs_resume  
4. `GoalService.resume_phase` + scheduler payload  
5. host 路由自动 resume + 文案  
6. 删除 slash 生命周期子命令与旧测试，补新测试  

## 13. 开放决策（实现前可再确认）

1. stop generation 默认 `needs_resume` 还是 `cancelled`（本文推荐前者）。  
2. `needs_resume` 自动失败上限次数（建议 3）。  
3. 快捷 `/goal obj --accept` 是否保留；若保留则仅为 init 糖，不参与生命周期叙事。

---

**一句话：** 阶段以 commit 为准；未 commit 就停在该阶段；用户只需说话，系统自动续跑——不把 goal 生命周期暴露成命令。