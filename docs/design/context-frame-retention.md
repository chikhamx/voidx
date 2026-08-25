---
name: context-frame-retention
display_name: Context Frame Retention and Physical GC
description: 限制每次 LLM 调用整包落盘的 context frame 数量，并在 tombstone 后物理删除孤儿文件
doc_type: tech-design
audience: human+llm
status: implemented
---

# Context Frame 保留与物理删除 — 技术设计文档

## TL;DR

`sessions/<sid>/context/` 现在是每次 LLM 调用的整包审计快照。删除只写 tombstone、清 SQLite 索引，jsonl 文件不删。实测一个 3 天会话就能堆出 3200 个文件、约 1.2GB，其中约 1.18GB 已无索引。

本方案不改 resume 路径，也不做增量编码。context frame 继续作为“那一次发给模型的 payload”快照，但每种 `frame_kind` 只保留最近 5 份，tombstone 时 unlink 对应文件，并提供一次 GC 清掉历史孤儿。

实现已落地：save 时按 kind 保留 5 份，tombstone 时 unlink，resume/compaction 对该 session 跑 GC。

## Context

### 当前数据流

每次真正调用 LLM 前，运行时把编译后的发送窗口整包写入：

| 调用方 | 入口 | `frame_kind` |
|--------|------|--------------|
| 主代理 | `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py` → `save_main_context_frame()` | `main` |
| 子代理 | `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | `worker` |
| 压缩代理 | `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py` | `compaction` |

`save_context_frame()` 做三件事：

1. 向 `context_frames` 插入轻量索引，拿到全局自增 `id`
2. 把 `file_path` 写成 `context/{id}.jsonl`
3. `write_session_records()` 把完整 messages 写成新文件

resume 不读这些文件。恢复走 `messages.jsonl` + `session_runtime_state`（含 Long Summary），再由 `ContextCompiler` 重新编译发送窗口。`load_context_frames()` 目前只给测试用。

### 实测证据

session `3cb0aea89645`（2026-08-19 至 2026-08-21）：

| 指标 | 值 |
|------|----|
| `context/*.jsonl` | 约 3200 个，1.2GB |
| SQLite 仍可见的 `context_frames` | 约 370 行 |
| 磁盘有、索引无的孤儿 | 约 2800 个，1.18GB |
| `context/deletes.jsonl` | 56 条，全是 compaction 的 `delete_messages_through` |
| 相邻两份 frame | 通常只多 1–2 条消息，但整份 system + 历史再写一遍 |

### 删除现状

`delete_messages_through()` / `delete_messages_from()` / `clear_messages()` 会：

- 写 `messages.jsonl` tombstone
- 写 `context/deletes.jsonl` 的 `context_frame_deleted`
- `DELETE FROM context_frames ...`

它们不 `unlink` jsonl。`user_message_id IS NULL` 的 worker / compaction frame 也不会被 `through` / `from` 的 SQL 删掉。只有 `/session del` 或 `delete_session()` 整目录 `rmtree` 才会物理删除。

### 测试已经超前于实现

`src/tests/test_agent/adapters/langgraph/runtime/test_session_context_frames.py` 已经要求：

- `test_save_context_frame_keeps_five_files_per_kind`：每种 `frame_kind` 磁盘上最多 5 份
- `test_delete_messages_through_unlinks_matching_context_files`：范围删除时 unlink 对应文件
- `test_gc_context_frames_removes_orphans_and_enforces_retention`：`gc_context_frames()` 删除孤儿，并把每种保留截到 5 份

当前 `context_frame_repository.py` 没有 `gc_context_frames`，`save_context_frame()` 也没有 retention trim。

这三份测试的 **keep=5 断言** 是验收标准，不能改数字来迁就实现。但 GC 测试的 **setup 和实现冲突**：它通过 `save_context_frame()` 连写 8 份 main，再断言 `removed >= 4`。若 save 当时就 trim 到 5，GC 只会删掉那 1 个孤儿，`removed >= 4` 必失败。

实现时必须改 GC 测试的 setup，不能改 keep=5：先用 save 留下 5 份 live，再直接插入额外 SQLite 行和 jsonl（绕过 save trim），模拟升级前的脏会话，然后 GC。

## Goals / Non-Goals

### Goals

- 每种 `frame_kind`（`main` / `worker` / `compaction`）每个 session 只保留最近 5 份完整 jsonl 和对应 SQLite 行。
- compaction / `/clear` / 范围删除在清索引时，同时 unlink 被删 frame 的 jsonl。
- 提供 `gc_context_frames(session_id)`：清无索引孤儿，并把超量 live frame 收到 5 份/kind。
- 现有长会话可被一次 GC 收回磁盘，不需要用户手动删目录。
- 保持 context frame 仍是“那一次真实发送窗口”的完整快照，便于排障。

### Non-Goals

- 不改 resume：继续用 `messages.jsonl` + `session_runtime_state`，不从 context frame 恢复。
- 不做增量 / delta / content-addressed 存储。`prefix_hash` 仍只用于稳定前缀标记，不用于复用文件。
- 不把 context frame 收成“一个当前窗口文件”。审计价值在于保留最近几次发送差异，单文件无法覆盖 retry / compaction / worker。
- 不改 Long Summary、消息 trim、tool output strip 的编译语义。frame 看起来“不完整”，是压缩后的发送窗口，不是存储丢失。
- 不在本方案压缩 `messages.jsonl`、`transcript.*` 或 `file-history/`。
- 不引入跨 session 的全局 context 配额；全局 id 继续自增。
- 不把 `load_context_frames()` 接到产品 UI。

## Proposed Design

分两层：热路径限流，冷路径收垃圾。

```text
LLM call
  -> save_context_frame()
       insert SQLite row
       write context/{id}.jsonl
       trim same session + frame_kind to newest 5
            unlink + delete older rows

compaction / clear / range delete
  -> SELECT matching rows (before DELETE)
  -> unlink their jsonl
  -> DELETE SQLite rows
  -> append context/deletes.jsonl tombstone

resume / compact / explicit repair
  -> gc_context_frames(session_id)
       unlink files not in live SQLite rows
       trim live rows per kind to 5
```

### Request / Data Flow

1. `save_context_frame()` 写入新 frame 后，立刻按 `(session_id, frame_kind)` 保留 `id` 最大的 5 行，其余 unlink + 删索引。
2. `delete_messages_through/from` 和 `clear_messages` 在 `DELETE FROM context_frames` 之前，先查出将要删除的 `file_path`，再 unlink。`user_message_id IS NULL` 的 worker/compaction 行：`through`/`from` 仍不按 message id 匹配；它们靠第 1 步的 per-kind cap 和 GC 回收。`clear_messages()` 内部 tombstone 为 `mode=all`，删除该 session 全部 frame 文件。
3. `gc_context_frames(session_id)` 扫描 `context/*.jsonl`（跳过 `deletes.jsonl`），删除不在 live 索引里的数字文件；再对每种 kind 执行与 save 相同的 5 份上限。返回删除文件数。
4. GC 触发点只有两处，都不扫全部 session：
   - `resume_session()` 恢复该 session 之后调用一次，用来收已有脏会话。
   - `persist_compaction()` 在 `delete_messages_through()` 之后再调用一次，用来收 compaction 刚制造的孤儿。
   `save_context_frame()` 只按 kind trim，不对整个 `context/` 做 `listdir`。
5. 不提供 `/session gc` 或启动时全局扫描。实现落地后，打开或压缩某个旧 session 就会回收它的磁盘。

### API / Function Contract

| Name | Input | Output | Error Behavior |
|------|-------|--------|----------------|
| `save_context_frame(record)` | 现有 `ContextFrameRecord` | 新 `frame_id` | 写文件失败则保留现状；trim 失败只记日志，不影响本次 LLM 调用 |
| `_trim_context_frames(session_id, frame_kind, keep=5)` | session + kind | 删除文件数 | 缺文件视为已删；SQLite 行仍删 |
| `gc_context_frames(session_id, keep_per_kind=5)` | session id | 删除文件数 | 忽略非数字 jsonl；`deletes.jsonl` 永不删 |
| `delete_messages_through/from` / `clear_messages` | 现有签名 | 现有语义 | unlink 失败不回滚 tombstone；下次 GC 再收 |

保留上限是实现常量，不做成用户配置：

```python
CONTEXT_FRAME_KEEP_PER_KIND = 5
```

5 来自已有测试，足够覆盖一次主循环里的 retry、最近一次 worker、最近一次 compaction。

### 文件删除规则

只允许删除 `sessions/<sid>/context/<digits>.jsonl`。

- 路径必须落在该 session 的 `context/` 下，`stem` 为十进制数字。
- 不删除 `context/deletes.jsonl`。
- 不删除 `messages.jsonl`、`transcript.*`、`runtime*.jsonl`、`subagents/`。
- unlink 使用 session jsonl lock，避免和正在写入的同 session 文件打架。

## Data Model / Migration

N/A。不改 `context_frames` schema，不改 jsonl 记录格式。

```text
context_frames          # 不变
├── id
├── session_id
├── user_message_id     # worker/compaction 常为 NULL
├── frame_kind          # main | worker | compaction
├── file_path           # context/{id}.jsonl
└── ...

context/{id}.jsonl      # 完整发送窗口；超出保留窗口后物理删除
context/deletes.jsonl   # tombstone 仍写，供 load_context_frames() 过滤
```

旧会话不迁移内容，只 GC 文件。`load_context_frames()` 继续先看 SQLite，再应用 tombstone；缺文件时 messages 视为 `[]`，不报错。

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| 每种 kind 保留 5 份完整快照，而不是只留 1 份当前窗口 | 单文件覆盖；按 token 预算保留 | 单文件无法看 retry / worker / compaction 差异；测试已锁定 5 |
| 继续整包落盘，不做 delta | 按 `prefix_hash` 复用稳定前缀；content-addressed chunk | 实现复杂，resume 又不读这些文件；先把 1.2GB 垃圾收回，收益最大 |
| tombstone 仍写，同时 unlink | 只 unlink 不写 tombstone；或继续只 tombstone | tombstone 是 messages/runtime 级联契约；unlink 解决磁盘。两者并存 |
| `user_message_id IS NULL` 的 frame 不靠 through/from SQL 删除 | 给 worker 补 user_message_id | 改调用链面更大；per-kind cap + GC 已能回收 |
| compaction 后和 resume 时对该 session 跑 GC，而不是每个 LLM 调用扫目录 | 每次 save 都扫目录；启动时扫全部 session | save 时按 kind trim 已够热路径；全目录扫描只在打开或压缩该 session 时做 |
| save 当时 trim；GC 测试用直接插行模拟脏会话 | 只 GC 不在 save 时 trim；或改 GC 测试的 keep=5 断言 | 热路径必须立刻封顶，否则未压缩的长会话照样堆文件。GC 仍要能收升级前的超量 live 行。允许改 GC 测试 setup，不允许改 keep=5 |
| 不把 retention 做成配置项 | `config.context_frame_keep` | 审计快照不是用户功能；先用测试锁定的 5 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| 排障时找不到很久以前的发送窗口 | 只能看最近 5 次/kind | 完整历史仍在 `messages.jsonl`；需要更久审计再另开开关 |
| unlink 与正在写入的 frame 竞争 | 偶发缺文件 | 走 session lock；trim 只删更旧的 id |
| `through` 删不掉 NULL `user_message_id` 的旧 worker 文件 | 压缩后 worker 文件暂时还在 | per-kind cap 会在后续 worker 写入时丢掉旧文件；GC 兜底 |
| 现有 1.2GB 脏会话不会在升级后立刻消失 | 未打开的旧 session 仍占磁盘 | resume 该 session 时 GC；`/session del` 仍整目录删除 |
| trim 失败被忽略 | 磁盘继续涨 | 测试覆盖成功路径；失败只影响空间，不影响对话 |

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `src/voidx/agent/adapters/persistence/context_frame_repository.py` | `save_context_frame()` 写入后 trim；新增 `gc_context_frames()`、内部 unlink helper | 常量 `CONTEXT_FRAME_KEEP_PER_KIND = 5` |
| `src/voidx/persistence/jsonl.py` | 新增删除单个 session 相对路径的 helper，走现有 session lock | 只接受 `context/<digits>.jsonl` |
| `src/voidx/agent/adapters/persistence/session_repository.py` | `clear_messages` / `delete_messages_from` / `delete_messages_through` 在 DELETE 前查出 `file_path` 并 unlink | tombstone 写入顺序保持不变 |
| `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py` | `persist_compaction()` 在 `delete_messages_through()` 之后调用 `gc_context_frames(session_id)` | 不要放到每次 LLM save 上 |
| `src/voidx/agent/adapters/langgraph/execution.py` | `resume_session()` 在 `restore_runtime_state()` 之后对该 session 调用 `gc_context_frames()` | 失败只记日志，不阻断 resume |
| `src/tests/test_agent/adapters/langgraph/runtime/test_session_context_frames.py` | 保留 keep=5 断言；改 GC 测试 setup，改为直接插入超量 live 行 + 孤儿文件后再 GC | 不要把 8 次 `save_context_frame()` 当成能攒出 8 份 live 的路径 |

### Existing Behavior

- 每次 LLM 调用新建 `context/{id}.jsonl`，SQLite 只存索引。
- 删除只写 `context/deletes.jsonl` 并 `DELETE FROM context_frames`。
- `load_context_frames()` 用 tombstone 过滤，默认最多返回 50 行。
- `delete_session()` 已经 `rmtree` 整个 session 目录。
- worker/compaction 常不带 `user_message_id`。

### Target Behavior

- 同一 session、同一 `frame_kind`，磁盘和 SQLite 都最多 5 份，保留 id 最大的。
- 范围删除 / clear 后，被删 frame 的 jsonl 不存在。
- `gc_context_frames()` 删除无索引数字 jsonl，并把 live 行收到 5/kind。
- `deletes.jsonl` 仍追加，旧 tombstone 语义不变。
- `load_context_frames()` 对已 unlink 的 live 行返回空 messages，不抛。
- LLM 调用失败不得因为 trim/GC 失败而失败。

### Invariants

- resume 仍然只依赖 `messages.jsonl` + `session_runtime_state`。
- context frame 仍是完整发送窗口，不是 delta。
- `context/deletes.jsonl` 继续存在，不能改成“删除即消失、无 tombstone”。
- 不得删除 session 目录下 context 以外的文件。
- `frame_kind` 取值保持 `main` / `worker` / `compaction`。
- 全局 `context_frames.id` 继续自增，不复用 id、不重命名文件。
- JSONL append-only 对 `messages.jsonl` / `deletes.jsonl` 仍然成立；被 GC 的是独立 snapshot 文件，不是追加日志本身。

### Edge Cases / Failure Paths

| Case | Expected Behavior | Test Coverage |
|------|-------------------|---------------|
| 第 6 个 `main` frame 写入 | 最小 id 的 main 文件和 SQLite 行消失；worker 不受影响 | `test_save_context_frame_keeps_five_files_per_kind` |
| `delete_messages_through` 命中带 `user_message_id` 的 frame | 对应 jsonl unlink，live frame 保留 | `test_delete_messages_through_unlinks_matching_context_files` |
| 磁盘上有无索引的 `999001.jsonl` | GC 删除它 | `test_gc_context_frames_removes_orphans_and_enforces_retention` |
| 升级前留下超过 5 份 live 行 + 孤儿 | GC 两者都收；setup 必须绕过 save trim | 同上；实现时只改 setup，不改 keep=5 断言 |
| worker `user_message_id is NULL` 遇到 `through` | SQL 不删这些行；后续 worker save / GC 按 kind cap 收 | 现有 through 测试不覆盖 NULL；实现时补一个 |
| `deletes.jsonl` 与数字 jsonl 同目录 | GC 永不删 `deletes.jsonl` | GC 测试已隐含；可显式断言 |
| jsonl 已缺、SQLite 行还在 | unlink 视为成功，继续删行 | 单元测试 |
| `clear_messages` | 该 session 全部 context jsonl 删除，索引清空，tombstone mode=`all` | 扩展现有 clear 测试或新增 |
| `delete_session` | 仍整目录删除，不走 per-file GC | 现有 session crud 测试 |

### Forbidden Changes

- 不要改 `ContextCompiler`、Long Summary 注入、tool trim。
- 不要让 resume 去读 context frame。
- 不要把 messages/transcript 改成可物理改写的日志。
- 不要把 retention 做成用户配置或 slash command。
- 不要引入新的 SQLite payload 列把 messages 搬回 DB。
- 不要修改已有测试里 `keep=5` 的断言来迁就实现。GC 测试只允许改 setup：直接插超量 live 行，而不是连写 8 次 save。
- 不要在热路径对整个 `context/` 做 `listdir`；save 时只按 kind 查 SQLite。

## Test Plan

| Scenario | Command / Check | Expected Result |
|----------|-----------------|-----------------|
| 现有 retention 测试 | `./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_session_context_frames.py` | save 后每种最多 5 份；through unlink；GC 清孤儿和超量 live 行。GC 测试 setup 改为直接插行 |
| 范围删除级联 | 同文件里原有 tombstone 测试 | load 仍过滤旧 frame；磁盘上旧文件不在 |
| compaction 回归 | `./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction.py` | 仍会保存 compaction/main frame；不依赖无限历史文件 |
| NULL user_message_id worker | 新增或扩展 context frame 测试 | `through` 不误删未来 worker；GC/cap 能收旧 worker 文件 |
| 手工脏会话 | 对 `3cb0aea89645` 调 `gc_context_frames` 后看 `du -sh context` | 从约 1.2GB 降到大约 5×3 份近期快照的量级 |

## Open Questions

- [x] 保留几份：5，与已有测试一致。
- [x] 是否做增量存储：不做，本轮只回收磁盘。
- [x] 现有脏会话的 GC：只在 resume 或 compaction 该 session 时回收，不在启动时扫全部 session。
- [x] worker 本轮不补 `user_message_id`。through/from 不匹配 NULL 行；靠 per-kind cap 和 GC 回收。
- [x] 不提供 debug 开关保留无限 frame。排障看 `messages.jsonl`。
- [x] `load_context_frames(limit=50)` 保持 50，避免无关 diff。
- [x] 不提供 slash/`/session gc`。GC 是内部维护。
- [x] 本文件只定方案。实现另开任务，先改 GC 测试 setup，再按 TDD 补 `gc_context_frames()` 和 save-time trim。
