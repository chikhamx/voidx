---
name: gateway-transcript-window
display_name: Gateway Transcript Window MVP
description: 已实现的 Gateway transcript 分页、窗口化 snapshot 和有界 JSONL 读取契约
doc_type: tech-spec
audience: human+llm
status: implemented
related_docs:
  - docs/design/cross-ui-performance-addendum.md
  - docs/archive/tui-long-session-performance.md
---

# Gateway Transcript Window MVP

> **Status: Done** — Archived on 2026-08-16.

## 1. 范围与目标

本 MVP 解决两个问题：

1. Gateway 在 thread 切换或分页请求时，可以只返回最近一段 turn；
2. 已建立索引的 transcript page 读取只访问目标 turn 的 JSONL 字节范围，不从目标 turn 扫描到文件尾。

2026-08-16 MVP 的兼容路径是：未传 `turn_limit` 的 `session.switch`，以及未建立窗口偏好的 legacy client，继续收到完整 `workspace.snapshot`。截至 2026-08-30，声明 `workspace_patch_v1` 且没有窗口偏好的 client 在普通广播中改收 `workspace.patch`；窗口偏好、显式 recovery 和首次连接仍按各自 snapshot 路径处理。

权威实现路径：

- `src/voidx/presentation/gateway/session/method/sessions.py`：方法参数校验和 RPC handler；
- `src/voidx/presentation/gateway/session/core.py`：windowed snapshot、客户端窗口偏好和广播；
- `src/voidx/presentation/protocol/v2/snapshot.py`：`ThreadSnapshot`/`WorkspaceSnapshot` DTO；
- `src/voidx/presentation/adapters/persistence/transcript_snapshot.py`：分页、index v2 和安全回退；
- `src/voidx/persistence/jsonl.py`：JSONL 范围 reader；
- `frontend/src/main.ts`：已实现的“加载更早 transcript page”调用和结果合并。

## 2. 已实现协议

### 2.1 `ThreadSnapshot` 字段

`workspace.snapshot` 的 `active_snapshot` 和 `transcript.page` 返回的 snapshot 使用以下字段。`session.switch` 不直接在 RPC result 中返回 snapshot；它触发后续 `workspace.snapshot` notification，其 `active_snapshot` 使用同一 DTO：

```json
{
  "thread_id": "thread-id",
  "revision": 12,
  "nodes": [],
  "windowed": true,
  "before_turn_id": 40,
  "after_turn_id": 59,
  "has_earlier": true,
  "has_later": false
}
```

字段语义：

| 字段 | 类型 | 语义 |
|---|---|---|
| `thread_id` | `string` | transcript 所属 thread；不是 opaque cursor。 |
| `revision` | `integer` | Gateway snapshot revision。 |
| `nodes` | `TranscriptNode[]` | 当前窗口内的节点；按已有 transcript tree DTO 编码。 |
| `windowed` | `boolean` | `true` 表示只返回 turn 窗口；`false` 表示完整 snapshot。 |
| `before_turn_id` | `integer\|null` | 返回页面中最早选中 turn 的 id；没有选中 turn 时为 `null`。 |
| `after_turn_id` | `integer\|null` | 返回页面中最晚选中 turn 的 id；没有选中 turn 时为 `null`。 |
| `has_earlier` | `boolean` | 当前页面之前仍有更早 turn。 |
| `has_later` | `boolean` | 当前页面之后仍有更晚 turn。 |

完整 snapshot 的 `windowed` 为 `false`；窗口字段保留默认值，不应被客户端当作分页游标使用。

### 2.2 `transcript.page`

方法已注册于 `GatewaySession._register_default_methods()`。

请求：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "transcript.page",
  "params": {
    "thread_id": "thread-id",
    "before_turn_id": 40,
    "turn_limit": 20
  }
}
```

参数：

- `thread_id`：必填，非空字符串；
- `before_turn_id`：可选，`integer` 或 `null`；缺省等同于 `null`；
- `turn_limit`：可选，整数 `1..50`；缺省为 `20`；`bool` 不被视为整数接受。

分页选择规则：

- `before_turn_id` 为 `null`：返回当前 thread 的最新 `turn_limit` 个 turn；
- `before_turn_id` 为 `B`：只选择 `turn_id < B` 的 turn，再取其中最新的 `turn_limit` 个；
- 返回的 `before_turn_id`/`after_turn_id` 是返回页面的首尾 turn id，不是请求参数回显；
- `transcript.page` 是只读操作，不改变 active thread、dock tree 或事件上下文；
- 方法结果是 `ThreadSnapshot.model_dump()`，因此 `windowed` 为 `true`。

### 2.3 `session.switch`

请求中的 `turn_limit` 为可选参数：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session.switch",
  "params": {
    "thread_id": "thread-id",
    "turn_limit": 20
  }
}
```

行为：

- 对 legacy client，不传或显式传 `null`：切换 thread 并发送完整 snapshot；
- 对声明 `workspace_patch_v1` 且没有窗口偏好的 client，不传或显式传 `null`：普通 switch 广播发送 metadata patch，不构造完整 transcript snapshot；
- 传 `turn_limit=1..50`：无论是否支持 patch，切换 thread 并发送 windowed snapshot；
- RPC result 仍返回 `active_thread_id` 和 `runtime_profile`；snapshot 路径中的 transcript window 位于后续 `workspace.snapshot` notification 的 `active_snapshot`；
- thread 不存在时返回 `-32000`；非法参数返回 `-32602`。

### 2.4 `workspace.snapshot` / `workspace.patch` 广播

Gateway 按 `ProtocolClient` 保存窗口偏好和 capability：

- client 通过 `session.switch.turn_limit` 或 `transcript.page.turn_limit` 建立窗口偏好；
- 有窗口偏好时，后续 snapshot 广播继续使用同一窗口大小；
- 没有窗口偏好且声明 `workspace_patch_v1` 时，普通状态广播发送 metadata-only `workspace.patch`；
- 没有窗口偏好的 legacy client 继续收到完整 snapshot；
- 多客户端的窗口偏好和 capability 相互隔离，断开时清理；
- `session.switch` 不传 `turn_limit` 会清除该客户端窗口偏好；后续编码由该 client 的 `workspace_patch_v1` capability 决定。

窗口偏好机制本身不是 capability negotiation，也不会把 session 全局窗口状态应用到所有客户端；2026-08-30 的 capability 分支是在该 MVP 之后新增的正交编码选择。

## 3. 参数错误与兼容约束

| 场景 | 错误码/行为 |
|---|---|
| `thread_id` 为空字符串、数组、对象、`true` 或其他非字符串 | `-32602` |
| `turn_limit` 不是整数、是 `bool` 或不在 `1..50` | `-32602` |
| `before_turn_id` 不是整数、是 `bool`，且不为 `null` | `-32602` |
| 合法但不存在的 thread | `-32000` |
| legacy client 未传 `session.switch.turn_limit` | 完整 snapshot，保持 2026-08-16 MVP 兼容行为 |
| `workspace_patch_v1` client 未建立窗口偏好 | 普通广播发送 `workspace.patch`；显式 snapshot/recovery 路径除外 |
| 任意 client 传合法 `turn_limit` | windowed `workspace.snapshot` |

客户端不得：

- 将 `before_turn_id` 当作 opaque cursor 或字符串 cursor；
- 假设默认窗口是 40 turns；当前默认值是 20，服务端允许范围是 1..50；
- 依赖 `transcript.page` 改变 active thread；
- 依赖窗口 snapshot 包含完整历史。

## 4. Transcript index v2 与有界读取

### 4.1 Index 结构

canonical `replace_transcript()` snapshot 和 index rebuild 会写入 v2 index，核心字段如下：

```json
{
  "version": 2,
  "transcript_size": 893666,
  "last_reset_offset": 0,
  "turn_offsets": {"1480": 661000},
  "turn_ranges": {"1480": [661000, 661450]},
  "summary_offsets": {},
  "range_readable": true,
  "indexed_end_offset": 893666,
  "last_checkpoint_offset": 893666,
  "last_checkpoint_path": "transcript.checkpoint.json"
}
```

`turn_ranges[turn_id]` 是半开区间 `[start, end)`：从该 turn 的 `turn_start` 行开始，到该 turn 的 `turn_end` 行结束之后。`turn_offsets` 保留为兼容和校验用途。

### 4.2 快速路径

`load_transcript_page()` 只有在以下条件全部满足时使用范围读取：

- index 存在且 `version == 2`；
- `index.transcript_size` 等于当前 `transcript.jsonl` 文件大小；
- `range_readable == true`；
- `indexed_end_offset` 等于当前文件大小；
- 页面内每个 turn 的 range 存在、连续合法，并且起点等于对应 `turn_offsets`。

此时调用 `read_session_records_between_offsets()`，只读取页面首个 turn 的起点到最后一个 turn 的结束边界。

### 4.3 安全回退

以下情况不得使用 bounded reader，或 bounded reader 返回失败后必须回退：

- v1 index、缺失 index 或 index/file size 不一致；
- `summary`、`node_update`、未知记录、未闭合 turn 或其他非 canonical 增量记录；
- index range 缺失、越界或与 `turn_offsets` 不一致；
- 范围内出现空行、提前 EOF、非法 UTF-8、非法 JSON 或非对象记录。

回退路径保持既有语义：优先利用 checkpoint、summary offset 或 turn offset；无法安全复用时从 JSONL 完整扫描并重建 index。范围 reader 遇到同文件大小的损坏行也必须失败，不能静默跳过并返回缺节点页面。

### 4.4 持久化不变量

- JSONL 写入成功后才写 index；
- `turn_start -> node* -> turn_end` 是 canonical turn 的记录顺序；
- index 缺失或失配时仍能恢复已有 JSONL；
- 损坏或不完整记录不能被当作完整 bounded page 成功返回；
- v1 index 可读，旧会话不要求迁移后才能恢复。

## 5. 前端使用约束

当前 frontend 在 `frontend/src/main.ts` 中按 `before_turn_id` 请求 `transcript.page`，使用 `turn_limit=TRANSCRIPT_PAGE_SIZE`，并将更早节点按 id 合并到现有 snapshot，同时保留 scroll anchor。

前端集成必须保持：

- 过期 thread context/generation 的 page response 不得安装；
- 已存在 node id 不重复插入；
- 页面没有更早内容时不继续请求；
- 用户正在查看历史时，加载更早页面不能跳回底部。

这些是当前 page consumer 的行为约束，不等同于未来完整的 DOM virtualization 或 keyed reconciliation 方案。

## 6. MVP 边界与后续状态

### 6.1 2026-08-16 MVP 已实现并延续

以下能力就是本 MVP 的完成范围，不是后续补做：

- numeric-turn window/page：`before_turn_id`、默认 20-turn、`has_earlier` / `has_later`；
- `session.switch.turn_limit`、`transcript.page` RPC 与 per-client window preference；
- index v2 bounded range read 及损坏/旧 index 的安全 full-scan fallback；
- Frontend 请求更早页面、按 node id 去重 prepend 并保持 scroll anchor。

### 6.2 2026-08-30 前后续已实现

- `stream_append_v1` / `workspace_patch_v1` capability negotiation、per-client legacy fallback、append/replace delta、workspace revision gap recovery；
- 支持 patch 的 client 在正常 submit/turn terminal 路径接收 metadata patch，而非完整 transcript snapshot；
- TUI viewport-first Rich render 与 TerminalWriter 慢 PTY/backpressure；
- 旧重复 transcript 的 canonical compaction。

### 6.3 仍未实现或未完整闭环

- `transcript_window_v1` 的端到端 capability 宣告、opaque cursor 和默认 40-turn window；
- P1 stream commit integrity hash；
- transcript canonical load+replace 的跨调用串行化与受生命周期管理的后台压实；
- TUI 完整 RenderPlan、活动 Markdown bounded projection；
- Desktop rAF batching、Markdown worker、完整 keyed reconciliation、DOM virtualization；
- durable live-history eviction。

后续能力不属于本 MVP 的完成声明；其权威状态分别记录于：

- `docs/design/cross-ui-performance-addendum.md`：仍为 `in-progress/partial`，继续负责跨端剩余项；
- `docs/archive/tui-long-session-performance.md`：TUI 运行时基线已完成并归档。

本 MVP 的归档状态不替代上述文档各自的验收边界。

## 7. 源码与验证

主要测试：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_transcript.py

./test.py --backend -- \
  src/tests/test_presentation/gateway/test_gateway_v2_routing.py
```

完整 backend 回归：

```bash
./test.py --backend
```

本次实现验证结果：

- transcript persistence：20 passed；
- Gateway v2 routing：41 passed；
- backend 全量：4742 passed, 30 skipped；
- `python3 -m py_compile src/voidx/persistence/jsonl.py src/voidx/presentation/adapters/persistence/transcript_snapshot.py`：通过；
- `git diff --check`：通过；
- 2000-turn 合成 page：20-turn 页面读取约 8,980 bytes，避免读取约 224,500 bytes 尾部。


## 8. 变更归属与归档边界

本文档是 2026-08-16 已实现并归档的 Gateway numeric-turn window MVP 契约记录，其完成状态不替代上游文档各自的验收：

- TUI 运行时基线已按自身边界完成并归档；
- 跨端增补仍为 `in-progress/partial`，只有其剩余 P0/P1/P2、统一 benchmark、慢路径观测和最终 verify 全部闭环后才可归档。
