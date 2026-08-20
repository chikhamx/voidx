---
name: goal-phase-transparent-resume
display_name: Goal 阶段检查点、三 Session 与透明续跑
description: Goal 使用 main、work、evaluator 三个 session，通过阶段专用协议工具和 durable checkpoint 实现透明续跑；用户不操作生命周期命令
doc_type: tech-design
audience: human+llm
status: draft
date: 2026-08-21
---

# Goal 阶段检查点、三 Session 与透明续跑

## 1. 结论

Goal 不再把 init、work、evaluator 当作若干不可恢复的瞬时调用，而是把它们定义为同一 generation 内严格线性的三个协议阶段。每个阶段的成功协议输出先写入 durable protocol journal，再由投影器按 `init → checkpoint → decision` 顺序提交 GoalState；任何中断都从日志中首个未提交边界开始线性重放，绝不越过前序边界或重复已经 durable 的副作用。

一个 goal-mode 主会话复用一个 `main session`；每个已批准的 `GoalSpec/generation` 创建一对长期复用的 `work session` 和 `evaluator session`。

```text
main session
├── initial / idle
├── 用户可见对话与 guidance
└── Goal 公共进度与结果摘要

Goal generation G1
├── evaluator session G1  （逻辑根，验收上下文）
└── work session G1       （执行子上下文）
```

三个 session 不共享原始 transcript，只通过结构化协议对象交互：

```text
main       -- GoalSpec / UserGuidance --> GoalRuntime
work       -- WorkCheckpoint ---------> GoalRuntime/evaluator
evaluator  -- GoalDecision -----------> GoalRuntime/main
GoalRuntime -- PublicSummary ---------> main
```

生命周期对用户完全透明：

- 不新增 `/goal continue`。
- 不把 `/goal start|stop|status|continue` 作为用户心智或主提示。
- 用户只描述目标、正常对话；系统记住停在哪并自动续跑。
- 三个阶段均可重放，但重放是线性的：先补齐或提交 init，再处理 checkpoint，最后处理 decision。
- `last AIMessage` 只是叙述或观察证据；专用工具调用形成协议输出，durable journal 是协议事实，GoalState 是按日志顺序得到的生命周期投影。

三个阶段只暴露各自的专用协议工具：

| 阶段 | 可见工具 | 唯一协议产出 |
|---|---|---|
| main initial / idle | `goal_init` | `GoalSpec` |
| work | `goal_checkpoint` | `WorkCheckpoint` |
| evaluator | `goal_decision` | `GoalDecision` |

本文是 Goal 生命周期、session、阶段协议和透明恢复的唯一设计规范。

## 2. 问题与范围

### 2.1 现象

1. work 或 evaluator 在阶段结果或 decision 未 commit 时因 LLM 退出、进程崩溃、lease 丢失等中断。
2. host 会话只能 `guidance queued`，或 stop 后进 idle，「继续」却不执行。
3. 重新启动 Goal 会 `new_generation()`，旧 work 上下文无法接上。
4. evaluator 以 detached/no-session 运行时没有可复用的评估记忆，或者错误加载 host/work 历史造成上下文污染。
5. UI/文案引导用户使用 `/goal status`、`/goal stop`，把内部调度暴露成操作手册。
6. 模型忘记报告阶段结果时，runtime 只能依赖不稳定的自然语言或直接把阶段判为失败。

### 2.2 根因

| 层 | 现状 | 后果 |
|---|---|---|
| runner | work → evaluator → 一次 attempt commit | 中段失败无法表达“停在 evaluator” |
| session | work、evaluator、host 的上下文边界不清晰 | 重试可能丢记忆或污染用户会话 |
| protocol | 一个通用 `goal(op=...)` 同时承担 init/decision | 阶段工具边界弱，模型容易提交错误协议 |
| recovery | side effect 后未 commit → `needs_user`，但 Goal 无按 phase 重入 | 状态可脏可停，不能续跑 |
| host 路由 | active → 只 guidance；非 active → idle | 可恢复 run 被当成无任务或死房间 |
| UX | 提示 status/stop | 用户被要求操作生命周期 |
| 重启/替换 | start 总是新 generation | 续跑被实现成另起炉灶 |

### 2.3 目标

1. 任一未完成 durable 边界的阶段都停在原阶段，并可自动恢复。
2. 一个 generation 内所有 work attempt 复用同一个 work session。
3. 一个 generation 内所有 evaluator attempt/resume 复用同一个 evaluator session。
4. main session 只保存用户上下文、guidance 和公共摘要，不被 work/evaluator 原始消息污染。
5. 每个阶段只暴露自己的协议工具，成功协议调用后立即结束当前 turn。
6. 模型忘记调用协议工具时自动修复；修复失败不误判为完成或 blocked。
7. 协议输出与 lifecycle 投影分离：三个阶段先 durable 写 journal，再按线性顺序重放投影，且不重复已经发生的副作用。

### 2.4 非目标

- 不从自然语言 `last AIMessage` 解析 `finished`、`continue`、`blocked`。
- 不让 evaluator 加载 work session 的完整历史。
- 不让 work 读取 main session 的完整聊天历史。
- 不为每个 turn、repair、resume 或 continue 创建新 generation/session。
- 不保留通用 `goal(op=...)` 作为兼容别名。
- 不把 loop 一并改成同样的透明模型。
- 不做已 `cancelled` 终态的 undo。
- 不引入用户可见的 phase 命令语言。

## 3. 用户可见契约

用户在 goal 模式下只需要：

1. **提出目标**：自然语言或现有 GoalSpec 审批流。
2. **运行中补充说明**：作为 guidance。
3. **中断后继续说话**：任意相关消息，含“继续”，系统自动从当前未完成阶段恢复。
4. **明确取消**：走通用中断/取消（Esc、stop generation、或明确“停下来”的取消语义），不是 `/goal stop` 教学。

系统向用户展示自然语言进度，例如：

- “目标进行中（实现阶段）……”
- “实现阶段已完成，正在验收……”
- “验收阶段中断，下一条消息将从验收继续……”
- “目标已完成 / 已阻塞：……”

**禁止**主路径文案出现：`Use /goal status or /goal stop`、`/goal continue`，或教用户 start/stop/continue。

### 3.1 `/goal` 表面收敛

| 输入 | 行为 |
|---|---|
| `/goal` | 仅切换到 goal profile |
| `/goal <objective> --accept <cond>` | 可选快捷启动（内部 init API），不作为生命周期模型宣传 |
| `/goal status\|stop\|start\|continue` | 删除，不做兼容别名 |

内部 `GoalService` 仍可有 `start`、`stop`、`resume_generation` 等方法，供 runtime 与路由调用，但不映射为用户 slash 生命周期。

## 4. 三 Session 拓扑

### 4.1 Session 身份与生命周期

`main session` 是 goal profile 的宿主会话，跨多个 generation 持续复用。每次在 main 的 idle 中批准新的 GoalSpec 时，为该 generation 创建一对 work/evaluator session：

```text
逻辑关系（不是目录关系）

main session
└── generation G1
    └── evaluator session G1  （逻辑根）
        └── work session G1   （执行子上下文）
```

session ID 也是 `<voidx-data-dir>/sessions/<session_id>/` 的目录名，因此 Goal 新建 ID 必须经过统一 `validate_session_storage_id()`：只允许小写 `[a-z0-9_-]+`，规范化后必须与原值完全相同；拒绝 `/`、`\\`、`:`、`..`、尾随点/空格、Windows 设备保留名（`con`、`prn`、`aux`、`nul`、`com1..com9`、`lpt1..lpt9`，含扩展名前缀）以及 parent id、workspace 路径或用户输入。单一小写字母表避免 Windows/macOS 默认大小写不敏感文件系统上的目录碰撞；所有 create/ensure/get/delete/session_dir 入口使用同一校验器，数据库另存 canonical ID 唯一约束。

```text
main_session_id:      ses_7m4k2q9v
work_session_id:      gws_01j8y4m6w5n8r2
evaluator_session_id: ges_01j8y4m6w5n8r3
generation:           gen_01j8y4m6w5n8r1
```

`goal:{parent}:{generation}:work` 只能用于日志标签或调试显示，不能作为 session ID、目录路径或恢复依据。实现必须持久化完整绑定，不能解析 ID 推导关系：

```text
main_session_id: str
evaluator_session_id: str
work_session_id: str
generation: str
```

`evaluator session` 是 generation 的逻辑根，`work session` 是关联执行子 session。父子关系只用于查询、归档和清理，**不表示目录嵌套或上下文继承**。

### 4.2 物理目录布局

沿用现有 `src/voidx/persistence/jsonl.py::session_dir()` 契约，所有 session 在磁盘上平铺。本文用 `<voidx-data-dir>` 表示 `src/voidx/platform/paths.py::voidx_home()` 的返回值；按当前实现它固定为 `~/.voidx`。环境变量 `VOIDX_HOME` 目前只被 `test.py`/`python.py` 用于定位 venv，不是 runtime 数据目录覆盖项，本文不得把两者混用。若未来支持数据目录 override，必须先统一修改 `voidx_home()`，再让 SQLite 与 session_dir 同时切换：

```text
<voidx-data-dir>/   # 当前为 ~/.voidx
├── store/
│   └── voidx.db
└── sessions/
    ├── ses_7m4k2q9v/             # main
    │   ├── messages.jsonl
    │   ├── runtime.jsonl
    │   ├── runtime_debug.jsonl
    │   └── context/
    │       ├── <frame_id>.jsonl
    │       └── deletes.jsonl
    ├── gws_01j8y4m6w5n8r2/      # work:G1
    │   ├── messages.jsonl
    │   ├── runtime.jsonl
    │   ├── runtime_debug.jsonl
    │   └── context/
    │       ├── <frame_id>.jsonl
    │       └── deletes.jsonl
    └── ges_01j8y4m6w5n8r3/      # evaluator:G1
        ├── messages.jsonl
        ├── runtime.jsonl
        ├── runtime_debug.jsonl
        └── context/
            ├── <frame_id>.jsonl
            └── deletes.jsonl
```

这些文件按需创建，不要求空 session 预建目录或空文件。每个目录只保存该 session 自己的 transcript、runtime 调试快照和 context frame：

| 文件 | 内容 | 是否权威恢复源 |
|---|---|---|
| `messages.jsonl` | 本 session 的 user/assistant/tool 消息 | 否；仅用于上下文恢复和审计 |
| `runtime.jsonl` | runtime state 删除标记等 session 级记录 | 否 |
| `runtime_debug.jsonl` | message runtime snapshot 与调试记录 | 否 |
| `context/<frame_id>.jsonl` | 压缩/冻结的上下文帧 | 否 |
| `context/deletes.jsonl` | context frame 删除标记 | 否 |
| `store/voidx.db` | session 元数据、generation 绑定、GoalState、journal、guidance、attempt、lease、outbox | **是** |

禁止在 work/evaluator 目录中复制 `GoalSpecSnapshot`、`GoalProtocolRecord`、`UserGuidance` 或 GoalState 作为第二份权威状态；这些对象只存 SQLite，构造 turn input 时按 generation/sequence 查询并注入。也禁止在 main 目录创建指向子 session 的 symlink、junction 或嵌套目录。

### 4.3 SQLite generation/session 绑定

新增 `goal_generations`，作为三 session 关系的唯一索引：

```sql
CREATE TABLE goal_generations (
    generation TEXT PRIMARY KEY,
    main_session_id TEXT NOT NULL,
    evaluator_session_id TEXT NOT NULL UNIQUE,
    work_session_id TEXT NOT NULL UNIQUE,
    goal_thread_id TEXT UNIQUE,
    visibility TEXT NOT NULL DEFAULT 'internal',
    created_at TEXT NOT NULL,
    terminal_at TEXT,
    archived_at TEXT,
    FOREIGN KEY (main_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
    FOREIGN KEY (evaluator_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
    FOREIGN KEY (work_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
);
CREATE INDEX idx_goal_generations_main
    ON goal_generations(main_session_id, created_at);
```

约束：

1. 边界 I 不调用当前会独立提交的 `ensure_session()`。profile/model snapshot 与两个安全 opaque ID 在事务外准备；随后在**同一个 SQLite 事务**中插入或确认 work/evaluator 两个 `sessions` 行、`goal_generations` binding、Goal thread/GoalState、INIT projected 状态和首个 work outbox。任一写入或约束失败整笔回滚，因此内部 session 不存在“已可见但尚未绑定”的窗口；目录仍按需创建，不属于该事务。
2. 已存在 generation 若任一绑定 ID 不同，必须 conflict；不得修补为新 ID。
3. 一个 work/evaluator session 只能属于一个 generation；main 可关联多个 generation。
4. `goal_thread_id` 在 Goal thread 创建后写入且不可换绑；恢复通过 generation 行定位三个 session，不扫描目录名。
5. `visibility=internal` 描述该 generation 的子 session 可见性，不隐藏 main。普通 session 查询必须排除 `sessions.id = goal_generations.work_session_id OR evaluator_session_id` 的行；即使 main_session_id 出现在 internal generation 中，main 仍正常出现在 `list_sessions()`、最近 session 和用户选择中。work/evaluator 仅通过 Goal generation 查询或显式内部诊断 API 可见。
6. 不复用 `provisional_sessions.root_session_id` 表达 Goal 关系；该表只表示 provisional 生命周期，不是持久父子关系。

### 4.4 Session 职责与上下文边界

| Session | 生命周期 | 允许写入 | 禁止加载 |
|---|---|---|---|
| `main` | goal profile 的整个宿主会话 | initial/idle 对话、用户 guidance、公共进度、最终摘要 | work/evaluator 原始 transcript |
| `work:G` | 一个 GoalSpec/generation，跨所有 attempt 复用 | 执行提示、工具调用、执行观察、checkpoint 工具调用 | main 完整历史、evaluator 完整历史 |
| `evaluator:G` | 一个 GoalSpec/generation，跨评估重试和 resume 复用 | evaluator 提示、只读验证、checkpoint 输入、decision 工具调用 | main 完整历史、work 完整历史 |

work session 可以加载自己之前 attempt 的历史，因为它需要知道已完成的工作；但每次继续执行必须以 workspace 当前状态为准，不能仅凭历史假设副作用已经存在。

evaluator session 可以加载自己之前的评估历史，因为它需要知道之前验证过什么；每次 evaluator turn 仍必须收到当前 GoalSpec 和最新 WorkCheckpoint，不能只依赖旧 session transcript。

三个 session 不共享原始 transcript：

- main 不接收 work/evaluator 的原始 LLM、工具和 repair 消息。
- work 不接收 main 的全量对话或 evaluator 的全量历史。
- evaluator 不接收 work 的全量历史，只接收结构化 checkpoint 和当次只读验证结果。

### 4.5 Transcript 写入、索引与 fencing

现有 JSONL helper 只有进程内 `asyncio.Lock`，不能单独保证跨进程安全；仅把 `fencing_token` 写进 JSONL 也不足以判断哪条消息已被 durable 接受。新增 SQLite 索引：

```sql
CREATE TABLE goal_transcript_records (
    session_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    local_sequence INTEGER NOT NULL,
    session_sequence INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL,
    filename TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (session_id, attempt_id, local_sequence),
    UNIQUE (session_id, session_sequence),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
    FOREIGN KEY (generation) REFERENCES goal_generations(generation) ON DELETE RESTRICT
);
CREATE INDEX idx_goal_transcript_order
    ON goal_transcript_records(session_id, session_sequence);
```

`SessionTranscriptWriter` 是 Goal work/evaluator transcript 的唯一写入口，提交顺序固定为：

```text
1. claim session 跨进程排他锁
2. 在 SQLite 同时校验 generation binding、cleanup tombstone、attempt lease 与 fencing token；pending/committed cleanup 一律拒绝
3. 检查 (session_id, attempt_id, local_sequence)：
   - 已存在且 payload_hash 相同 → 返回原 accepted record
   - 已存在但 hash 不同 → conflict
4. 将确定性 JSON record 编码为 UTF-8 bytes，并追加单个 `b"\n"`；以二进制 `ab+` 的 byte position 记录 [start_offset, end_offset)，flush + fsync
5. SQLite 事务再次校验 binding/cleanup/fencing，分配该 session 的下一 `session_sequence`，插入 accepted byte-offset index，并原子递增 message_count
6. 释放 session lock
```

无法跨 JSONL 与 SQLite 做单一事务，因此采用“文件先写、索引后接受”：

- 进程在第 4 步后退出会留下未索引 orphan JSONL 行；hydration、message_count 和 context frame 构建都忽略它。
- retry 使用同一 `(session_id, attempt_id, local_sequence)`。orphan 仅在持锁状态下同时匹配 session、filename、generation、attempt_id、attempt_number、local_sequence、payload_hash、完整 JSON record boundary 和当前适用 fencing 时才能复用；否则追加新记录。最终只能存在一个 accepted key。
- writer 若发现尾部 partial record（无完整换行、UTF-8/JSON 不完整或 key/hash 不匹配），在持锁状态下将其截断到最后一个已验证 record boundary，或移入诊断隔离文件；不得将 partial 区间写入 accepted index。
- 进程在第 5 步后退出已经安全；retry 返回原 accepted record，不重复进入 canonical transcript。
- 旧 fencing runner 即使曾通过第 2 步，只要在第 5 步前失去 lease或遇到 cleanup pending，第二次校验失败，其 JSONL 行保持 orphan。

读取规则：

1. main session 沿用普通 `messages.jsonl` hydration；work/evaluator session 必须按 `goal_transcript_records.session_sequence` 升序取得 accepted offsets，再通过 `read_session_records_between_offsets()` 精确读取。`attempt_id` 仅用于幂等，不参与跨 attempt 排序；`attempt_number/local_sequence` 用于审计和不变量校验。
2. offset 越界、hash 不匹配、文件截断或 accepted row 指向无效 JSONL 时，不跳过继续；非终态 generation 进入 durable runtime failure，终态/归档 generation 标记审计损坏。
3. context frame 只能由 accepted transcript records 构造；`context_frames.metadata_json` 必须记录 covered accepted keys/range。不能压缩或冻结 orphan 行。
4. session `message_count` 只统计 accepted records，并与 accepted index 在同一个 SQLite 事务更新；不得按 JSONL 物理行数计算。
5. JSONL append、accepted index 或 context frame 写入失败都不改变 protocol journal/GoalState；phase 保持可恢复，SQLite 协议事实永远优先于 transcript 观察。

跨进程 lock 的实现必须兼容 macOS/Linux/Windows；若当前文件锁基础设施无法满足，替代方案是由单一 persistence writer 进程串行落盘，但不能退回“假设不会多进程”。

### 4.6 Generation 边界

- main session 可以承载多个 generation。
- 每次在 idle 中批准新的 GoalSpec，创建新的 generation、work session 和 evaluator session。
- 同一个 generation 的 `continue`、resume、协议 repair 不创建新 session 或目录。
- 用户明确创建新目标时，旧 generation 进入终态或取消；只有此时才允许 `new_generation()`。
- 旧 generation 的 work/evaluator transcript、checkpoint 和 decision 不进入新 generation 的 prompt。
- 一个 generation 只有一个 work session 和一个 evaluator session；一个 work/evaluator session 不能跨 generation 复用。


### 4.7 Generation bundle 的归档与清理

一个 generation 的 runtime state、两个内部 session 和目录构成不可拆分的 `GoalGenerationBundle`：

```text
GoalGenerationBundle(G)
├── goal_generations[G]
├── Goal thread / GoalState
├── protocol journal / guidance / attempts / outbox / failure records
├── work session row + sessions/<work_session_id>/
└── evaluator session row + sessions/<evaluator_session_id>/
```

main session 不属于 bundle，删除/归档一个 generation 不删除 main，也不删除 main transcript 中已经发布的 PublicSummary。

归档规则：

1. generation 只有进入 `completed | blocked | failed | cancelled` 后才可归档。
2. 归档在 SQLite 事务中写 `archived_at`，并使该 bundle 不再参与 active/resumable 查询；work/evaluator session 行和目录仍保留，供审计或显式诊断。
3. 归档不移动目录、不重命名 session ID，也不把 transcript 合并到 main；物理移动会破坏 `session_dir(session_id)` 和 context frame `file_path` 契约。
4. archived bundle 可由 retention policy 进入清理，但不得通过通用 session age cleanup 单独选中其 work/evaluator session。

清理必须以 generation bundle 为单位，并使用 durable tombstone 协调 SQLite 与文件系统：

```text
active/archived
  → cleanup_pending      # SQLite 事务：写 tombstone，禁止查询/恢复/新写入
  → delete directories  # 幂等删除 work/evaluator 两个平级目录
  → cleanup_committed    # SQLite 事务：删除内部 session 行与 generation runtime rows
```

具体约束：

1. 新增独立 tombstone 表；它不以待删除 generation/session 行为外键，committed 后仍保留定位信息：

```sql
CREATE TABLE goal_generation_cleanup (
    generation TEXT PRIMARY KEY,
    cleanup_epoch INTEGER NOT NULL,
    main_session_id TEXT NOT NULL,
    work_session_id TEXT NOT NULL,
    evaluator_session_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | committed
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
```

2. `cleanup_pending` 事务先复制 generation 与三个 session ID 到 tombstone，递增 `cleanup_epoch`，确认 generation 已终态，并取消/封禁未完成 lease、attempt 和 outbox。之后 writer 的两次 SQLite 校验均因 pending/committed tombstone 而拒绝。
3. cleanup 与 transcript writer 使用同一跨进程 session lock。cleanup 按 canonical session ID 字典序获取 work/evaluator 两把锁，writer 每次只获取一把；任何需要多把 session 锁的流程都遵守相同排序，禁止反向获取。这样 pending 事务会阻止尚未入场 writer，cleanup 等待已持锁 writer 退出。
4. cleanup 持有两把锁期间，幂等删除 work/evaluator 两个平级目录、确认其不存在，并执行最终 SQLite 清理事务；释放锁前再次根据 tombstone ID 检查目录仍不存在。append helper 在持锁后的 tombstone 校验前不得 `mkdir`，因此迟到 writer 不能重建目录。
5. 最终 SQLite 事务按外键顺序执行：先删除 `goal_transcript_records`、guidance/attempt/outbox/journal/failure 和 Goal thread 等引用行；再删除 `goal_generations` binding；最后删除 work/evaluator 两个 internal `sessions` 行。`context_frames`、session runtime state 等 session 子表可由现有 session FK cascade 清理；随后将独立 tombstone 标为 committed。不得先删 session row 后依赖重试绕过 `ON DELETE RESTRICT`。
6. 任一步失败保留 `pending + last_error` 并重试；不得回滚成 active，也不得重新创建已删除目录。即使 binding/session row 已由人工修复删除，reconciler 仍可凭 tombstone 中的两个 opaque 子 ID 定位并清除迟到复活目录。
7. 用户删除 main session 时，先枚举其所有非 committed bundle：运行中 generation 必须明确取消并完成终态事务；随后逐 bundle cleanup；最后才删除 main 目录和 session row。禁止依赖 `sessions` 外键级联静默遗留子目录。
8. 通用 `delete_session(internal_session_id)` 必须拒绝；只有 `delete_goal_generation(generation)` 和 main-session bundle 删除流程能删除 Goal 内部 session。
9. orphan reconciler 定期检查：binding 有 session row 但目录缺失、目录存在但 internal session row 缺失、cleanup tombstone 卡住或 committed 后目录复活。若 session 的 `message_count=0`、无 context frame 且无 accepted transcript index，目录尚未按需创建是合法状态；否则终态 bundle 可继续清理，非终态 bundle 的 canonical transcript/context 缺失则进入 durable runtime failure，不凭空创建替代历史。
## 5. 内部状态机与线性日志

### 5.1 线性阶段序列

每个 generation 的合法协议序列只有一种：

```text
INIT(0)
  → CHECKPOINT(1)
  → DECISION(1)
      ├─ continue → CHECKPOINT(2) → DECISION(2)
      ├─ finished → terminal
      └─ blocked  → terminal
```

`attempt_number` 从 1 开始；init 固定使用 0。任何阶段都必须等前一条协议记录完成投影后才能执行。scheduler、recovery 和手工内部 API 均不得跳过该约束。

```text
main idle
  │ goal_init 获批准并写入 INIT(0)
  ▼
投影边界 I：创建 generation 与 session 绑定
  ▼
work          current_phase=work, phase_status=running
  │ goal_checkpoint 写入 CHECKPOINT(N)
  ▼
投影边界 A：保存 WorkCheckpoint，切到 evaluator
  ▼
evaluator     current_phase=evaluator, phase_status=running
  │ goal_decision 写入 DECISION(N)
  ▼
投影边界 B：attempt_count=N
  ├─ finished  → Goal completed + main idle + 公共摘要
  ├─ continue  → current_phase=work，执行 CHECKPOINT(N+1)
  └─ blocked   → Goal blocked 终态 + main idle + 阻塞摘要
```

运行中断后不直接根据内存中的 `current_phase` 猜测恢复点。recovery 读取该 generation 的 journal 与 GoalState 投影版本，找到首个未投影或尚不存在的期望记录，严格按线性序列处理。

### 5.2 Durable protocol journal

新增持久化记录 `GoalProtocolRecord`，它是协议事实的唯一来源：

```text
GoalProtocolRecord
- protocol_id: str
- parent_session_id: str
- generation: str
- phase: init | checkpoint | decision
- attempt_number: int                 # init=0，其余从 1 开始
- sequence_number: int                # init=0；checkpoint(N)=2N-1；decision(N)=2N
- turn_id: str
- session_id: str                     # init=main；其余为对应阶段 session
- payload_type: GoalSpecSnapshot | WorkCheckpoint | GoalDecision
- payload: typed JSON
- status: submitted | projected
- payload_hash: str
- submitted_at: datetime
- projected_at: datetime | null
```

generation 在 `goal_init` 获批、写 INIT record 之前生成，并连同 `parent_session_id`、冻结 spec 和 intake idempotency key 一起写入 journal；此时尚不创建 Goal thread、work session 或 evaluator session。它们只在边界 I 根据该 generation 的确定性 ID 幂等创建。因此 INIT submitted 后即使进程退出，恢复仍能定位同一 generation，而不会再次 `new_generation()`。

唯一约束与幂等规则：

1. `protocol_id` 全局唯一。
2. `(generation, sequence_number)` 唯一，确保一个线性位置只有一个成功输出。
3. `(generation, phase, attempt_number)` 唯一，便于阶段查询和校验。
4. 相同位置、相同 `payload_hash` 的重复调用返回原记录；相同位置、不同 payload 必须报 conflict，不能覆盖。
5. 记录一旦为 `submitted` 就不可修改 payload，只能推进为 `projected`。
6. INIT 写入前使用 main-session intake lease、稳定 intake idempotency key 和 parent session 校验；此时不要求 Goal thread、子 session、phase attempt 或 generation runner lease 已存在。
7. CHECKPOINT/DECISION 写入前必须校验前序记录已 `projected`，且 generation、session、attempt 与 generation lease/attempt fencing token 一致；不能只依赖模型可见工具。

journal 必须位于与 Goal thread state、attempt、outbox 相同的 durable store 中，不能只保存在 controller、TurnResult 或进程内存中。controller 只持有本 turn 已写入 journal 的记录引用。

### 5.3 三个 durable 投影边界

| 边界 | journal 输入 | 原子投影内容 |
|---|---|---|
| I. init | `INIT(0)` submitted | 冻结 GoalSpecSnapshot；创建或确认 generation、work/evaluator session 绑定和 Goal thread；写初始 GoalState；将 INIT 标为 projected；入队 `CHECKPOINT(1)` 的 work outbox |
| A. work | `CHECKPOINT(N)` submitted | 保存 `last_work_checkpoint`；设置 `current_phase=evaluator`、`phase_status=running`；将记录标为 projected；入队同 N 的 evaluator outbox；`attempt_count` 不变 |
| B. evaluator | `DECISION(N)` submitted | 保存 decision；设置 `attempt_count=N`；将记录标为 projected；continue 时切回 work 并入队 N+1，finished/blocked 时写 Goal 终态和 PublicSummary outbox |

每个边界必须在一个 store transaction 中完成 GoalState CAS、journal `submitted → projected` 和后继 outbox 写入。边界 I 还必须在该同一 SQLite transaction 中插入/确认两个 internal `sessions` 行和 `goal_generations` binding；禁止先调用会独立提交的 session provisioning API。若 persistence port 目前不能共享 connection/transaction，必须先扩展事务 API，不能以可重复 ensure 替代原子性。事务重试使用冻结且稳定的两个子 session ID；相同绑定幂等，不同绑定 conflict。

`attempt_count` 只在边界 B 成功后变化。因此 evaluator 已运行但 decision 未投影时，当前 attempt 仍未完成。

协议调用的提交顺序固定为：

```text
专用工具参数校验
  → store 原子写入 GoalProtocolRecord(status=submitted)
  → controller 保存 record 引用
  → terminal barrier，TurnResult 返回 protocol_id
  → projector 原子提交边界并标记 projected
```

工具只有在 `submitted` 记录 durable 后才返回成功。这样即使进程在工具成功后立即退出，也能仅凭 journal 重放投影，不需要重新调用模型。

### 5.4 GoalState 字段目标形状

在 `GoalState`（`src/voidx/agent/domain/automation/goal.py`）增加或收敛为：

```text
run_id: str
generation: str
main_session_id: str
work_session_id: str
evaluator_session_id: str
projected_sequence_number: int = -1
current_phase: Literal["work", "evaluator"] = "work"
phase_status: Literal["running", "needs_resume"] = "running"
last_work_checkpoint: WorkCheckpoint | None = None
last_protocol_id: str = ""
interrupt_reason: str = ""
protocol_repair_count: int = 0
```

现有 `attempt_count`、`max_attempts`、evaluator 摘要字段保留。`projected_sequence_number` 必须与最后一条 projected journal 记录一致，用于 CAS 与恢复校验。`active` 删除；运行中与终态以 Goal lifecycle 判定函数为唯一来源。

不再使用 `phase_status=committed` 这种瞬时状态；提交事实由 journal status 和 `projected_sequence_number` 表达，持久状态只需 `running | needs_resume`。

## 6. 阶段专用协议工具

不再暴露通用 `goal(op=...)`。每个 phase 只暴露一个能产生该 phase 协议结果的工具，服务端仍必须做 phase 校验，不能只依赖模型看到了哪些工具。

### 6.1 `goal_init`

**可见范围：** main session 的 initial 和 idle phase。

**职责：** 将用户意图转换为经用户批准的 GoalSpec；不执行任务，不提交 work/evaluator 状态。

目标输入 schema：

```json
{
  "objective": "string",
  "acceptance_condition": "string",
  "achievement_method": "string",
  "max_attempts": 20
}
```

行为：

1. 校验 objective、acceptance_condition 和 attempts。
2. 发起现有 GoalSpec 审批流程。
3. `approved`：以冻结的 GoalSpecSnapshot durable 写入 `INIT(0, submitted)`；controller 只保存 record 引用，当前 initial/idle turn 进入协议终止屏障。
4. `revise`：不保存 spec，返回修改意见，允许同一 turn 再次调用 `goal_init`。
5. `cancel`：controller 标记取消，当前 turn 正常结束，不创建 GoalRun。

`goal_init` 不接受 `status`、`evidence`、`reason`、`next_hint` 等执行或验收字段。initial 与 idle 共用 `goal_init`，不复制成 `goal_reinit`。

### 6.2 `goal_checkpoint`

**可见范围：** work session。

**职责：** 报告本次 work turn 的可验证结果，形成边界 A 的输入。

目标输入 schema：

```json
{
  "summary": "string",
  "evidence": ["string"],
  "changed_files": ["string"],
  "verification": ["string"],
  "next_hint": "string",
  "progress": "none | partial | meaningful"
}
```

协议产出：

```text
WorkCheckpoint
- protocol_id
- generation
- attempt_number
- source = model | runtime_fallback
- completeness = complete | incomplete
- summary
- evidence
- changed_files
- verification
- next_hint
- progress
- work_turn_id
- observed_assistant_summary?
- observed_tool_result_summaries?
- created_at
```

调用成功后：

1. 将 typed `WorkCheckpoint` durable 写入当前线性位置 `CHECKPOINT(N, submitted)`；controller 保存 record 引用。
2. 当前 work turn 立即结束，不再发起下一次 LLM。
3. projector 重放边界 A，原子更新 GoalState、标记 record projected 并创建 evaluator outbox。
4. scheduler 使用已投影 checkpoint 创建 evaluator 输入。
5. `attempt_count` 不递增。

`last AIMessage` 和 runtime 自动观察到的 tool summaries 只能作为 `observed_*` 辅助字段，不能覆盖模型声明的 checkpoint，也不能单独把 work 标记为完成。

### 6.3 `goal_decision`

**可见范围：** evaluator session。

**职责：** 依据 GoalSpec、WorkCheckpoint 和只读验证结果提交唯一生命周期决策。

目标输入 schema：

```json
{
  "status": "finished | continue | blocked",
  "summary": "string",
  "evidence": ["string"],
  "reason": "string",
  "next_hint": "string",
  "missing_evidence": ["string"],
  "progress": "none | partial | meaningful"
}
```

协议产出：

```text
GoalDecision
- protocol_id
- generation
- attempt_number
- status
- summary
- evidence
- reason
- next_hint
- missing_evidence
- progress
- created_at
```

调用成功后：

1. 将 typed `GoalDecision` durable 写入当前线性位置 `DECISION(N, submitted)`；controller 保存 record 引用。
2. 当前 evaluator turn 立即结束，不再发起下一次 LLM。
3. projector 重放边界 B，原子更新 GoalState、标记 record projected 并创建唯一合法后继。
4. `finished`：Goal 进入 completed 终态并回到 main idle。
5. `continue`：Goal 回到 work，复用当前 generation 的 work session。
6. `blocked`：Goal 进入 blocked 终态并回到 main idle。

在 evaluator 决策路径中，只有 durable `GoalDecision` 可以产生 continue/completed/blocked lifecycle 投影；evaluator 的普通文本不能改变 lifecycle。runtime 仅可按 9.2、9.3 明确定义的 failure/cancel 事务进入 failed/cancelled，不得伪造 GoalDecision 或跳过 durable 记录。

### 6.4 协议终止屏障

协议工具成功后必须设置当前 turn 的 terminal barrier：

```text
成功的专用工具调用
  → durable 写 GoalProtocolRecord(status=submitted)
  → controller 保存 record 引用并标记 protocol_submitted
  → 当前 turn break
  → 返回 TurnResult.protocol_id
  → projector 按 sequence 重放 durable 边界
```

协议工具调用后的额外自然语言不能覆盖 typed output；额外 tool call 不能改变已提交的 checkpoint/decision。

工具参数校验失败不触发终止屏障：

```text
无效参数
  → 返回工具错误
  → controller 没有 protocol output
  → 同一 turn 允许模型修正并再次调用
```

同一个 `protocol_id` 只允许一个成功的 typed output；重复调用返回已保存结果或幂等成功，不能产生第二个 decision。

### 6.5 `last AIMessage` 的定位

`TurnResult.final_assistant_summary` 可以继续从最后一个 AIMessage 提取，但语义只能是 `observed_assistant_summary`。它可以作为 checkpoint 观察字段、repair prompt 上下文或 PublicSummary 候选；不能代替 `goal_checkpoint`、代替 `goal_decision`、被解析成 lifecycle，或在协议缺失时直接推进 GoalState。

## 7. Session 间数据流

### 7.1 main → GoalRuntime

main 在批准 `goal_init` 后只发送冻结的 `GoalSpecSnapshot`：

```text
GoalSpecSnapshot
- objective
- acceptance_condition
- achievement_method
- max_attempts
- generation
- profile_snapshot
- model_snapshot
```

运行中用户消息不直接拼进 work/evaluator transcript，而是变为：

```text
UserGuidance
- generation
- target_phase = work | evaluator | any
- text
- source = user | system
- created_at
- consumed_at
```

“继续”不是特殊命令，只是普通 input；路由器根据 `phase_status=needs_resume` 选择恢复 phase。

### 7.2 GoalRuntime → work

work turn 的输入包括：

```text
- GoalSpecSnapshot
- current attempt_number
- 上一次 GoalDecision 的 summary/next_hint/missing_evidence（continue 时）
- 当前 generation 尚未消费的 UserGuidance
- workspace 当前状态提示
```

不得注入 main 全量聊天历史、evaluator 全量 transcript 或其他 generation 的内容。

work 完成后必须通过 `goal_checkpoint` 产出 WorkCheckpoint。

### 7.3 GoalRuntime → evaluator

evaluator turn 的输入包括：

```text
EvaluatorInput
- GoalSpecSnapshot
- attempt_number
- WorkCheckpoint
- 当前 generation 的 evaluator session 历史
- 当前 generation 尚未消费、目标为 evaluator/any 的 UserGuidance
```

evaluator 可以使用 read/find/search/lsp/document 等只读工具检查 checkpoint 中的证据，但不允许执行写操作或 shell 命令。

不得注入 work session 的完整 transcript、main session 的完整 transcript 或其他 generation 的 checkpoint/decision。

### 7.4 GoalRuntime → main

只发布用户可见的 `PublicSummary`：

```text
PublicSummary
- generation
- phase
- outcome
- objective_summary
- attempt_count
- summary
- created_at
```

不把 evaluator 工具调用、内部 repair prompt、原始 work 工具日志或模型内部推理写入 main session。

## 8. 线性恢复、协议遗漏与 Guidance 投递

### 8.1 恢复算法

恢复单位是 generation 的完整协议序列，不是孤立 phase。`resume_generation(generation)` 必须在 generation 级互斥锁或 lease 下执行：

```text
1. 读取 GoalState、全部 GoalProtocolRecord、未完成 attempt/outbox
2. 校验 sequence 连续、唯一约束及 projected_sequence_number
3. 从 sequence=0 开始找到首个未完成位置：
   a. 已有 submitted、未 projected：只重放对应投影边界
   b. 记录不存在：确认前序已 projected，幂等确保该位置唯一的 phase outbox 存在；不直接启动模型
   c. 已 projected：继续扫描下一位置
4. 每完成一个投影重新读取/CAS，再处理唯一合法后继
5. 遇到 finished/blocked/cancelled 立即停止；不得创建后继 outbox
```

若日志存在空洞、同序号冲突、GoalState 投影领先于 journal，恢复必须停止并进入 `needs_user`/内部告警，不能猜测或跳过。多个 recovery worker 依靠 generation lease、state version CAS、journal 唯一键与 outbox 唯一键实现幂等；输掉竞争者重新读取。

phase 启动只有一条合法链路：projector/recovery 幂等写入或确认 `(generation, sequence_number)` 唯一 outbox；dispatcher claim outbox lease 后创建或复用稳定 `attempt_id`/input frame；runner 只执行被 claim 的 attempt。recovery 不得绕过 outbox 调用 runner，projector 也不得直接启动模型。outbox redelivery 复用相同 attempt/input frame，并由 attempt fencing token 防止两个 runner 同时产生有效协议记录。

### 8.2 三阶段中断矩阵

| 中断点 | journal/投影状态 | 线性恢复动作 |
|---|---|---|
| `goal_init` 批准前中断 | 无 INIT | 留在 main；下一条消息在同一 main session 继续 intake，不创建 generation |
| INIT 已 submitted，边界 I 前失败 | INIT submitted | 重放边界 I，幂等确认 generation 与两个 session，再启动 CHECKPOINT(1)；不再次审批 |
| INIT 已 projected，work 未产出 | INIT projected，无 CHECKPOINT(1) | 复用 work session 执行 attempt 1；workspace 当前状态优先 |
| CHECKPOINT(N) 已 submitted，边界 A 前失败 | checkpoint submitted | 只重放边界 A；不重跑 work，不重复副作用 |
| 边界 A 完成，evaluator 未产出 | checkpoint projected，无 decision | 复用 evaluator session，只跑 DECISION(N) |
| DECISION(N) 已 submitted，边界 B 前失败 | decision submitted | 只重放边界 B；不重跑 evaluator |
| DECISION(N)=continue 已 projected | decision projected | 线性进入 CHECKPOINT(N+1)，复用 work session |
| finished / blocked / cancelled | Goal 终态 | 不自动恢复；main idle 对话或新 init |

硬约束：

- 所有 resume 先 replay journal，再决定是否调用 LLM。
- resume 禁止 `new_generation()`、新 session id 或丢弃 session 绑定。
- 后序 phase 禁止在前序 record 未 projected 时启动。
- 已 submitted 的协议输出禁止通过重新调用模型替换。
- work repair/恢复不得盲目重复已经发生的不确定副作用；必须检查 session 历史与 workspace 当前状态。

### 8.3 模型忘记调用协议工具

control loop 检测当前阶段没有 journal record 时，向同一个 session 注入 repair prompt，最多自动修复 2 次。修复次数不是 Goal attempt 次数；一旦已有 submitted record，必须跳过 repair 并进入投影 replay。

#### main initial/idle

```text
没有 INIT record
  → 提醒调用 goal_init 或提出一个澄清问题
  → 仍无 INIT：留在 main，不创建 GoalRun
```

批准操作必须携带稳定的 intake `turn_id`/idempotency key。审批成功后重试相同 init 位置返回原 INIT record，不再次弹审批；revise/cancel 不写 INIT。

#### work

```text
没有 CHECKPOINT(N)
  → 提醒调用 goal_checkpoint
  → 修复期间禁止重复写操作，优先只允许报告和只读工具
```

如果 work 已正常返回且 runtime 观察到可靠工具结果，但模型仍未调用 checkpoint，可以写入该线性位置的 fallback record：

```text
source=runtime_fallback
completeness=incomplete
```

fallback 同样先 durable submitted，再走边界 A；它必须交给 evaluator，不能直接判定 Goal 成功。若没有可靠观察证据，则保持 `current_phase=work`、`phase_status=needs_resume`、`interrupt_reason=missing_work_checkpoint`，且不创建后序记录。

#### evaluator

```text
没有 DECISION(N)
  → 提醒调用 goal_decision
  → 仍无 decision：current_phase=evaluator，phase_status=needs_resume
```

不能从 evaluator 普通自然语言生成 fallback decision。

### 8.4 工具参数无效与提交失败

参数 validation error 不写 journal、不触发 terminal barrier，模型可在当前 turn 修正。达到 repair 上限后按当前阶段缺失协议处理。

journal 写入失败时工具必须返回失败，不能对模型声称提交成功，也不能推进 GoalState。journal 已 submitted 但投影失败时，这不是模型遗漏：recovery 按 sequence 重放边界；只有确认该位置没有 durable record 时才允许重进 LLM phase。

### 8.5 Guidance 的可靠投递

`UserGuidance` 是 durable inbox 记录：

```text
UserGuidance
- guidance_id: str
- generation: str
- target_phase: work | evaluator | any
- text: str
- source: user | system
- created_at: datetime
- delivered_attempt_id: str | null
- delivered_phase: work | evaluator | null
- consumed_at: datetime | null
```

采用至少一次投递、attempt 内去重：

1. host 先 durable 写入 guidance，再触发 resume；写入失败则明确提示未记录。
2. scheduler 在创建 phase attempt/input frame 的同一事务中，选择匹配且未绑定的 guidance，将其绑定到稳定 `attempt_id`，并把 `guidance_id` 与文本快照写入 input frame。
3. phase session 以 `guidance_id` 去重；相同 attempt 重放使用同一 input frame，不重新选择 guidance。
4. 只有对应协议记录完成 projected 后，才在同一边界事务中将该 attempt 绑定的 guidance 标记 `consumed_at`。
5. attempt 在协议提交前失败时，guidance 保持绑定但未消费；恢复复用相同 attempt/input frame，因此不会丢失，也不会被另一个 phase 抢走。
6. `target_phase=any` 在首次绑定后即固定目标 phase；后续不得再次投递给另一阶段。
7. input frame 冻结后到达的 guidance 不得追加到正在运行的 attempt，也不得抢占它；它保持未绑定，直到下一个尚未冻结且 target 匹配的 attempt。`target_phase=any` 因而绑定到到达后的下一个 phase，而不是追溯绑定当前 phase。

main intake 的用户原始消息属于 main transcript；若还需要作为 generation guidance，边界 I 必须以新的 `guidance_id` 显式写入，不能依赖 transcript 隐式继承。

### 8.6 `missing_goal_decision` 新语义

缺失 DECISION record 不是 blocked：保持 `current_phase=evaluator`、`phase_status=needs_resume`、`interrupt_reason=missing_goal_decision`，lifecycle 为 `needs_user`，且 `attempt_count` 不变。用户下一条消息触发完整线性恢复；扫描到 DECISION(N) 缺失时，只幂等确保该位置的 evaluator outbox，再由 dispatcher/runner 复用 evaluator session 执行。

连续 resume 超过策略上限时仍不得直接 blocked：保持 `needs_user` 并发布可诊断的人工介入摘要。只有 evaluator 写入并投影 durable `GoalDecision(status=blocked)` 才能进入 blocked。若检测到不可恢复的存储损坏或状态不变量破坏，则按 9.2 的 runtime failure 事务进入 failed，而不是伪造 evaluator decision。

## 9. Host 路由、Goal 终态与取消

### 9.1 透明续跑路由

改写 `src/voidx/agent/adapters/input_router.py` 中 `InputRouter.route_followup` 的 goal 分支：

```text
status = goal_service.status(parent)

if status is resumable:
    if user_input.strip():
        submit_guidance(user_input)   # 可选；空则纯 resume
    await goal_service.resume_generation(parent)
    提示自然语言：从哪一阶段继续
    return

if status is actively running:
    submit_guidance(user_input)
    简短确认已记录补充说明（不提 slash）
    return

# 无活跃/可恢复 Goal
idle turn（可调用 goal_init）
```

可恢复条件至少包括：`phase_status=needs_resume`，或 lifecycle 为 `needs_user` 且 generation/session 绑定仍完整。

要点：

- 可恢复时不得落入 idle-only 对话却不推进 phase。
- running 时消息仍是 guidance，不抢占当前 phase runner。
- “继续”不是特殊命令，只是普通消息触发 resume。
- 用户明确提出新目标时，才结束/替换旧 generation 并创建新 generation。

### 9.2 Goal 终态契约

Goal 领域的终态集合固定为：

```text
GOAL_TERMINAL_LIFECYCLES = completed | blocked | failed | cancelled
```

当前通用 `TERMINAL_LIFECYCLES` 不包含 `blocked`，因此 GoalService、Goal scheduler、恢复扫描、状态查询、归档和 session 清理不得继续直接使用通用集合判断 Goal 是否活跃；统一调用 `is_goal_terminal(lifecycle)`。本设计不修改通用集合，避免无意改变 loop 或其他 thread 对 blocked 的语义。

边界 B 投影 durable `GoalDecision(status=blocked)` 后必须：停止生成后继 outbox、从 active Goal 索引移除该 generation、发布一次阻塞摘要，并让 main 回到 idle。blocked generation 不自动 resume；用户若要继续，必须经新的 `goal_init` 创建新 generation。`GoalState.active` 删除，禁止同时维护布尔 active 与 lifecycle。

`failed` 不是模型协议结果，只允许用于 runtime 确认无法安全重放的内部故障，例如 journal/GoalState 不变量损坏、持久化数据不可解析或 fencing 安全性失效。进入 failed 前必须 durable 写 `GoalRuntimeFailure(generation, observed_sequence, reason, evidence, created_at)`；随后在一个 store transaction 中 CAS lifecycle=failed、取消未 claim 的后继 outbox、阻止 submitted record 继续投影，并写 PublicSummary outbox。普通模型遗漏、repair/resume 超限、临时 store/lease 失败都只能保持 needs_resume/needs_user，不能进入 failed。

### 9.3 取消

| 来源 | 结果 |
|---|---|
| 通用 stop generation / Esc 等运行时取消 | 默认当前 phase → `needs_resume`，避免一断即死 |
| 用户明确放弃目标 | `cancelled` 终态 → main idle + 摘要 |
| 内部 `GoalService.stop` | 仅供系统，不映射用户 slash |

如果现有 stop generation 无法区分生成中断和明确取消，优先 `needs_resume`。取消投影必须与取消 outbox/lease、终态 lifecycle 和 PublicSummary 在同一 store transaction 中提交；已 submitted 但未 projected 的协议记录保留用于审计，但 cancelled 后不得继续投影。

## 10. 组件职责

| 组件 | 路径 | 职责变化 |
|---|---|---|
| Goal domain models | `src/voidx/agent/domain/automation/goal.py` | GoalSpecSnapshot、GoalState、WorkCheckpoint、GoalDecision、sequence 计算和 `is_goal_terminal` |
| Thread/runtime contracts | `src/voidx/agent/domain/thread.py`, `src/voidx/agent/domain/turn_context.py`, `src/voidx/agent/application/runtime/contracts.py` | phase、attempt/input frame、typed protocol record 引用；不改变通用 terminal 集合 |
| Persistence port/adapter | `src/voidx/agent/ports/persistence.py`, `src/voidx/agent/adapters/persistence/thread_repository.py` | GoalProtocolRecord、GoalRuntimeFailure、UserGuidance、Goal generation/cleanup/transcript index、唯一键、CAS 和原子事务 |
| Goal tools | `src/voidx/agent/adapters/tools/automation/goal.py` | 删除通用 GoalTool，新增 `goal_init`、`goal_checkpoint`、`goal_decision`；工具成功前 durable 写 journal |
| Goal control protocol | `src/voidx/agent/adapters/langgraph/runtime/control_protocol.py` | 按 phase 注入专用 schema/controller，校验线性位置，处理 repair 和 terminal barrier |
| Turn control | `src/voidx/agent/adapters/langgraph/runtime/core/turn.py` | journal submitted 后立即 break；TurnResult 只携带 protocol record 引用 |
| Goal controllers | `src/voidx/agent/application/automation/goal/controller.py` | 分离 intake/checkpoint/decision controller；以 generation/sequence 幂等提交 journal |
| GoalRuntimeRunner | `src/voidx/agent/application/automation/goal/runner.py` | 只执行 dispatcher 已 claim 且 fencing 有效的 attempt，处理 fallback，不接受 recovery 直接调用，不直接跳 lifecycle |
| GoalEvaluator | `src/voidx/agent/application/automation/goal/evaluator.py` | 使用独立 evaluator session；只消费 checkpoint 和只读验证结果 |
| GoalService | `src/voidx/agent/application/automation/goal/goal_service.py` | 创建与恢复三 session 绑定、`resume_generation`、Goal 专用终态判定和公共摘要 |
| GoalRuntimeScheduler | `src/voidx/agent/application/automation/goal/scheduler.py` | 在事务中创建稳定 attempt/input frame、绑定 guidance、生成唯一后继 outbox |
| Goal projector（新增） | `src/voidx/agent/application/automation/goal/projector.py` | 按 sequence 投影边界 I/A/B；原子更新 state、record、outbox、guidance 和终态摘要 |
| RuntimeDispatcher / Recovery | `src/voidx/agent/application/runtime/dispatcher.py`, `src/voidx/agent/application/runtime/recovery.py` | recovery 只 replay 投影或确保唯一 outbox；dispatcher 是 claim outbox 并启动 runner 的唯一入口 |
| Host 输入路由 | `src/voidx/agent/adapters/input_router.py` | durable 写 guidance 后自动 resume；删除 status/stop 教学文案 |
| Session registry | `src/voidx/agent/adapters/persistence/session_repository.py` | opaque ID 校验、internal visibility 过滤、generation 绑定查询，并拒绝单独删除 Goal 子 session |
| Session transcript storage | `src/voidx/persistence/jsonl.py`, `src/voidx/agent/adapters/persistence/session_repository.py`, `src/voidx/agent/adapters/persistence/context_frame_repository.py` | 保持平铺目录；实现跨进程锁、双重 fencing、accepted offset index、幂等写入和 canonical hydration |
| Session cleanup | `src/voidx/agent/adapters/persistence/session_cleanup.py`, `src/voidx/agent/application/automation/goal/cleanup.py`（新增） | 通用 cleanup 排除 internal session；Goal 以 generation bundle 执行 tombstone、目录删除和 orphan reconciliation |
| Schema migration | `src/voidx/persistence/migrations.py` | 新增 protocol journal、runtime failure、guidance、goal_generations、goal_transcript_records、cleanup tombstone 及唯一索引 |
| Slash `/goal` | `src/voidx/presentation/slash/commands/mode.py`, `src/voidx/presentation/slash/registry.py` | 仅 profile 切换和可选快捷 init；删除生命周期子命令 |

## 11. 数据流示例：三阶段线性重放

```text
1. main 的 goal_init 获批准
   → journal durable 写 INIT(0, submitted)
   → turn terminal barrier
   → 进程退出

2. recovery 扫描到首个未投影位置 INIT(0)
   → 重放边界 I
   → 幂等创建/确认 generation G1、work:G1、evaluator:G1
   → INIT(0) projected + CHECKPOINT(1) outbox 原子提交
   → 不重新审批、不重新调用 goal_init

3. work:G1 执行并调用 goal_checkpoint
   → journal durable 写 CHECKPOINT(1, submitted)
   → turn terminal barrier
   → 边界 A 原子保存 checkpoint、标记 projected、入队 DECISION(1)

4. evaluator:G1 调用 goal_decision(status=continue)
   → journal durable 写 DECISION(1, submitted)
   → 边界 B 提交失败

5. 用户在 main 发送任意相关消息
   → durable 写 UserGuidance(U1)
   → resume_generation(G1) 从 sequence 0 扫描
   → INIT(0)、CHECKPOINT(1) 已 projected
   → DECISION(1) submitted：只重放边界 B
   → attempt_count=1，入队 CHECKPOINT(2)
   → 不重跑 init/work/evaluator

6. scheduler 创建 work attempt 2
   → 在同一事务把 U1 绑定到 attempt/input frame
   → work:G1 执行 CHECKPOINT(2)
   → CHECKPOINT(2) projected 时将 U1 标记 consumed

7. evaluator:G1 产出 DECISION(2, finished)
   → 边界 B 原子写 completed、停止后继 outbox、发布 PublicSummary
   → main 回到 idle，可调用 goal_init 创建 G2
```

## 12. 风险与策略

| 风险 | 缓解 |
|---|---|
| 工具成功后进程退出导致协议丢失 | 工具返回成功前 durable 写 GoalProtocolRecord；恢复只依赖 journal |
| 多 worker 重复投影或越序执行 | generation lease + state CAS + sequence/journal/outbox 唯一键；每次只处理首个未完成位置 |
| work 续跑重复 side effect | 已 submitted checkpoint 只重放投影；缺失 checkpoint 时结合固定 input frame、session 历史和 workspace 状态检查 |
| init 重放重复审批或创建 session | intake idempotency key + INIT 唯一位置；边界 I 使用确定性 ID 幂等 ensure |
| guidance 在崩溃窗口丢失或重复 | durable inbox；与 attempt/input frame 原子绑定；协议 projected 后才消费；按 guidance_id 去重 |
| evaluator 上下文污染 main/work | evaluator 使用独立持久 session，只接收 GoalSpec、checkpoint 和只读验证结果 |
| evaluator session 历史跨目标污染 | evaluator session 按 generation 创建；新 generation 只注入当前 generation 数据 |
| blocked 被通用集合误判为 active | Goal 统一使用 `is_goal_terminal`，不直接依赖通用 `TERMINAL_LIFECYCLES` |
| “继续” vs “换新目标” | 默认可恢复则 resume；明确新目标时才创建新 generation |
| 自动 repair 死循环 | 每个 turn 最多 2 次协议 repair；已有 submitted record 时禁止 repair |
| runtime fallback 被误当完成 | fallback 明确 `source=runtime_fallback`、`completeness=incomplete`，仍需 evaluator decision |
| 主会话压缩丢失恢复信息 | journal、GoalState、guidance、outbox、attempt/input frame 是权威来源，不依赖 main transcript |
| store schema 迁移失败 | migration 先建 journal/guidance 唯一索引，再切读写；新旧 Goal run 不混用 |
| session ID 路径字符、Windows 保留名或大小写碰撞 | 统一小写 `[a-z0-9_-]+` canonical ID，拒绝设备名/尾随点空格；所有 session 存储入口共用校验器 |
| Goal 子 session 污染普通 session 列表 | `goal_generations.visibility=internal`，所有 list/recent/delete 查询统一过滤 |
| JSONL 进程内锁无法防跨进程并发写 | transcript writer 校验 attempt fencing 并获取跨进程锁；失效消息 hydration 时过滤 |
| 目录与 SQLite 不能原子删除 | generation bundle tombstone 两阶段清理；幂等删除并由 reconciler 续作 |
| 单独删除子 session 留下半个 generation | 通用 delete 拒绝 internal session，只允许 bundle cleanup |
| session 目录成为第二份生命周期真相 | 目录只存 transcript/context；journal、guidance、GoalState 与绑定仅存 SQLite |
| slash 删除破坏脚本 | 明确 breaking；脚本改走 profile + 自然语言/init |

## 13. 验收标准

1. main initial/idle 只看到 `goal_init`，work 只看到 `goal_checkpoint`，evaluator 只看到 `goal_decision`。
2. 每个成功协议调用在返回前都有 durable `GoalProtocolRecord(submitted)`；controller 内存清空后仍可恢复。
3. 一个 generation 的记录严格为 `INIT(0), CHECKPOINT(1), DECISION(1), ...`，空洞、重复位置和越序调用均被拒绝。
4. init submitted 后任一崩溃点只重放边界 I，不再次审批，并且只存在一个 generation、一对 work/evaluator session 和一个 work outbox。
5. checkpoint submitted 后任一崩溃点只重放边界 A，不重跑 work；decision submitted 后只重放边界 B，不重跑 evaluator。
6. 同一 protocol id 或相同位置/相同 payload 的重试幂等；相同位置/不同 payload 返回 conflict。
7. 两个 recovery worker 并发恢复时只有一个投影和一个后继 outbox，另一方 CAS 失败后安全重读。
8. 新 GoalSpec 复用 main session，但创建新的 generation、work session 和 evaluator session；同 generation 的所有 attempt/resume 复用对应 session。
9. evaluator 只接收 GoalSpec + WorkCheckpoint + 自身 session 历史，且不具备写工具和 shell 工具。
10. `last AIMessage` 不会单独写 journal、推进 lifecycle 或代替 checkpoint/decision。
11. work 漏 checkpoint 时最多 repair；可靠观察可形成 incomplete fallback record，无证据则 work/needs_resume 且不产生后序记录。
12. evaluator 漏 decision 时最多 repair，之后 evaluator/needs_resume，attempt_count 不变，不能从自然语言生成 decision。
13. guidance durable 写入后才向用户确认；attempt 创建时原子绑定，崩溃恢复复用同一 input frame，projected 前不丢失、projected 后不重复消费。
14. `target_phase=any` 只投递一次；同一 guidance_id 在 session 中重复出现不会重复应用；input frame 冻结后到达的 guidance 只绑定到下一个匹配 attempt。
15. `attempt_count` 仅随 DECISION(N) 边界 B 投影为 N；checkpoint 和 repair 不递增。
16. `completed | blocked | failed | cancelled` 对 Goal 都是终态；blocked 不会被 status、scheduler 或 recovery 当作 active。
17. `needs_resume` 不创建新 generation/session；finished/blocked/cancelled 不自动 resume。
18. main session 不出现 work/evaluator 原始 transcript、repair prompt或内部工具日志。
19. 主路径无 start/stop/continue/status 教学，`/goal status|stop|continue` 不存在。
20. journal/GoalState 不一致、sequence 空洞或 payload conflict 会停止恢复并产生可诊断错误，不猜测推进；进入 failed 时存在 durable GoalRuntimeFailure，且 failure/state/outbox/PublicSummary 原子提交。
21. recovery 与 dispatcher 并发时，recovery 只确保唯一 outbox；只有持有 outbox lease 和有效 fencing token 的 dispatcher attempt 可以启动 runner、提交协议。
22. 边界 I 在任一 SQL 写入点失败时，两个子 session、binding、GoalState、INIT projection 和首个 outbox 全部回滚；普通 session 查询从未观察到未绑定子 session。
23. outbox redelivery 复用同一 attempt_id/input frame；旧 lease runner 即使晚到也无法写入有效 checkpoint/decision。
24. blocked 只能来自 durable evaluator decision；repair/resume 策略耗尽保持 needs_user，不能直接 blocked/failed。
25. 三个 session 的目录都直接位于 `<voidx-data-dir>/sessions/<opaque_session_id>/`（当前 `<voidx-data-dir>=~/.voidx`），不存在 main/evaluator/work 的物理嵌套、symlink 或通过 ID 解析的关系。
26. session/generation ID 只包含小写 `[a-z0-9_-]`，拒绝 Windows 设备名、大小写非 canonical 输入和尾随点/空格；Windows/macOS/Linux 上均通过同一 validator 与 `session_dir(session_id)` 创建、读取和删除。
27. `goal_generations` 对 generation、work session、evaluator session 保持唯一约束；绑定冲突停止恢复，不分配替代 ID。
28. 普通 list/recent/auto-title/delete candidate 只排除命中 work/evaluator 绑定的 session；main 即使关联 internal generation 仍可见。内部诊断可按 generation 读取两个子 session。
29. session 目录只含本 session 的 messages/runtime/context 文件；删除目录不会丢失 journal/GoalState 权威事实，篡改 transcript 也不能推进 lifecycle。
30. 两个进程或旧/新 fencing runner 同时写同一 Goal session 时，只有具有 accepted byte-offset index 的有效 attempt 消息进入 canonical hydration；幂等键为 `(session_id, attempt_id, local_sequence)`，跨 attempt 顺序只由唯一 `session_sequence` 决定。
31. generation 归档只写 `archived_at`，不移动或合并目录；归档后不参与 active/resumable 查询。
32. bundle cleanup 与 writer 共享按 canonical ID 排序的跨进程锁；在只删除一个子目录后崩溃可以依据 tombstone 续作，committed 后迟到 writer 不能重建目录，最终无 internal session row、Goal runtime row、子目录或可恢复 outbox。
33. 通用 `delete_session(work/evaluator)` 被拒绝；删除 main 时先终结并清理其全部 generation bundle，再删除 main。
34. orphan reconciler 能区分合法的未创建空目录、canonical transcript 缺失、孤儿目录、卡住 tombstone 和 committed 后复活目录；即使 binding/session row 已删除，仍可用 tombstone 保存的子 ID 清理。
35. canonical writer 使用二进制 UTF-8 + 单个 LF 计算真实 byte offsets；进程在 JSONL fsync 后、accepted index 前崩溃只留下 orphan 行，恢复后 hydration/message_count/context frame 均忽略它，重试最多产生一个 accepted key。
36. accepted index 指向截断、越界或 hash 不匹配的 JSONL 时，非终态 generation 进入 durable runtime failure，不静默跳过损坏消息。
37. cleanup 最终事务遵守 RESTRICT 外键顺序：先删 transcript/runtime 引用，再删 generation binding，最后删两个 internal session；任一步失败整体回滚。
38. 不同 attempt 的 opaque attempt_id 排序不会影响 transcript；hydration 严格按 session_sequence 还原接受顺序。
39. session ID 大小写变体、Windows 保留名与尾随点/空格均在创建目录前被拒绝；不会出现两个 SQLite ID 映射同一目录。
40. cleanup pending 与已入场 writer 交错时，锁顺序无死锁；cleanup committed 后 tombstone 仍能定位并删除任何迟到复活目录。

## 14. 建议实现顺序

1. 在 `src/voidx/agent/domain/automation/goal.py` 新增 GoalSpecSnapshot、WorkCheckpoint、GoalDecision、GoalProtocolRecord、GoalRuntimeFailure、UserGuidance、GoalGenerationBinding/Cleanup 与 sequence/Goal 终态函数。
2. 扩展 `src/voidx/agent/ports/persistence.py`、`src/voidx/agent/adapters/persistence/thread_repository.py` 和 `src/voidx/persistence/migrations.py`，让边界 I 在一个 shared SQLite transaction 中创建两个 internal session、binding、Goal state/journal/outbox；同时实现 guidance/failure、`goal_transcript_records`、cleanup tombstone 和唯一索引。
3. 改造 `session_repository.py`：新增统一小写 canonical ID/Windows 保留名校验，按 work/evaluator binding 过滤 internal session，提供内部查询并拒绝单独删除 Goal 子 session。
4. 改造 `jsonl.py`/context persistence：实现二进制 UTF-8 byte offset、session_sequence、双 fencing、跨进程共享锁、partial/orphan 处理和 canonical hydration；幂等键使用 `(session_id, attempt_id, local_sequence)`。
5. 实现三个专用工具和 controller，使工具成功前写 submitted record，并接入 terminal barrier。
6. 新增 `src/voidx/agent/application/automation/goal/projector.py`，按 sequence 投影边界 I/A/B，原子更新 state、record、outbox 和 guidance。
7. 改造 recovery/dispatcher：从 sequence 0 扫描并只 replay 投影或确保唯一 outbox；dispatcher claim 后才允许 runner 执行；覆盖三个阶段全部崩溃窗口。
8. 改造 scheduler/runner，创建稳定 attempt/input frame、原子绑定 guidance，只执行 dispatcher 已 claim 且 fencing 有效的 attempt。
9. 创建并复用独立 work/evaluator session，落实 evaluator 只读隔离及三 session transcript 边界。
10. 新增 `src/voidx/agent/application/automation/goal/cleanup.py`，实现持久身份 tombstone、与 writer 共用的有序双锁、bundle archive/cleanup、main 删除协调和 orphan reconciliation；通用 session cleanup 排除 internal session。
11. 改造 GoalService 与 `InputRouter.route_followup`，使用 Goal 专用终态、durable guidance、透明 `resume_generation` 和 PublicSummary。
12. 删除通用 goal 工具和旧 slash 生命周期子命令；新增 `src/tests/test_goal/test_goal_protocol_journal.py`、`test_goal_recovery.py`、`test_goal_guidance.py`、`test_goal_session_storage.py`、`test_goal_cleanup.py`，覆盖边界 I 全事务回滚、ID 大小写/Windows 保留名、跨 attempt session_sequence、Windows byte offset、partial/orphan、cleanup/writer 交错、committed 后目录复活、晚到 guidance 和 outbox redelivery。

## 15. 已确定决策

以下决策已确定：

1. stop generation 默认 `needs_resume`；只有用户明确放弃目标才是 `cancelled`。
2. 每个 turn 的协议 repair 上限暂定为 2 次；连续 resume 超过策略上限后保持 `needs_user` 并请求人工介入，不直接 blocked/failed。
3. initial 与 idle 共用 `goal_init`。
4. evaluator session 按 generation 创建并复用；不使用 detached/no-session 作为正式 evaluator 模型。
5. 快捷 `/goal <objective> --accept <cond>` 可以保留，但只作为 init 糖，不参与生命周期叙事。

实现完成后，本文应作为唯一 Goal 设计文档；不得再维护独立的 Goal phase protocol 文档。

## 16. 验证命令

实现时先创建第 14 节列出的五个新测试文件；完成后运行 journal、投影、session storage、cleanup 和崩溃恢复定向测试：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/persistence/test_thread_store.py \
  src/tests/test_goal/test_goal_protocol_journal.py \
  src/tests/test_goal/test_goal_recovery.py \
  src/tests/test_goal/test_goal_guidance.py \
  src/tests/test_goal/test_goal_session_storage.py \
  src/tests/test_goal/test_goal_cleanup.py
```

再运行 Goal 协议、runner、evaluator、service 与 control protocol 回归：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/tools/test_goal_tool.py \
  src/tests/test_agent/adapters/langgraph/test_control_protocol_registry.py \
  src/tests/test_domain/test_control_protocols.py \
  src/tests/test_goal/test_goal_protocol.py \
  src/tests/test_goal/test_goal_runner.py \
  src/tests/test_goal/test_goal_evaluator.py \
  src/tests/test_goal/test_goal_service.py
```

```bash
./test.py --backend -- \
  src/tests/test_goal \
  src/tests/test_agent/adapters/langgraph \
  src/tests/test_application/test_goal_idle_turn.py \
  src/tests/test_application/test_mode_dispatch.py
```

最后运行：

```bash
./test.py --backend
```

预期结果：三阶段 submitted 后崩溃均只重放投影；并发恢复不重复；guidance 不丢失、不跨 phase 重复消费；三 session 使用平铺安全目录且 internal session 不污染普通列表；跨进程/旧 fencing 写入不进入 canonical transcript；bundle 半删除可续作且无孤儿状态；Goal 定向与 runtime 回归全绿，最后 backend 全套通过。

---

**一句话：** main、work、evaluator 使用三个平铺且隔离的 session 目录，关系与生命周期只由 SQLite 绑定和线性 journal 定义；init、checkpoint、decision 从首个未投影位置严格重放，用户只需正常对话即可透明续跑。
