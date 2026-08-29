---
name: cross-ui-performance-addendum
display_name: Cross-UI Performance Addendum
description: 补充 TUI 长会话方案未覆盖的活动流增长、桌面端全量 snapshot/DOM 重建、终端背压和端侧内存问题
doc_type: tech-design
audience: human+llm
status: in-progress
implementation_status: partial
related_docs:
  - docs/design/tui-long-session-performance.md
---

# 跨端长会话性能优化增补方案

## 1. 决策摘要

`docs/design/tui-long-session-performance.md` 已处理三条基础主线：

1. `OutputTree` 的全历史渲染与尾部增量；
2. transcript 的全量持久化与周期 checkpoint；
3. `UiEventBus` 的公平调度与安全合并。

这些改造是必要条件，但还不能完整解决长会话卡顿。原方案主要控制“**已提交历史 H**”的成本，未覆盖另外三种增长维度：

- **A：当前活动内容大小**——单个 assistant stream、tool result、prompt 或输入草稿持续增长；
- **D：端侧展示状态大小**——桌面 WebView 的 DOM、Markdown、高亮、布局和 scroll 状态持续增长；
- **B：同步 I/O 与传输字节**——Gateway 大 snapshot、TUI stdout、PTY/SSH 回压和退出导出阻塞事件循环。

本增补按依赖关系分三阶段：

1. **P0：端侧热路径止血，不改变协议语义**
   - TUI 在 Rich 转换前按 viewport 裁剪，增量处理活动 Markdown，并将 terminal 写入移出 asyncio 主循环；
   - Desktop 将累计全文更新收敛到每帧一次，只重建未闭合 Markdown 尾块，避免强制滚动布局；
   - 同一 TUI frame 只计算一次 panel/status/thinking/input 布局。
2. **P1：将全量 snapshot 降级为连接与恢复路径**
   - 引入 capability negotiation；支持的客户端使用 stream append delta 和 `workspace.patch`；
   - 正常 submit/turn terminal 状态不再发送完整 transcript；
   - Desktop 使用 keyed reconciliation 和按 turn 分页/窗口化；revision 失配时回退完整 snapshot。
3. **P2：限制 live history 与本地输入/I/O 常驻成本**
   - transcript durable 后，TUI 可驱逐已经进入 scrollback 的旧 root turns；
   - 大批输入一次性插入，paste 使用可变缓冲，候选查询异步化或索引化；
   - 退出先恢复 terminal，再异步生成兼容 `transcript.log`。

**新增核心不变量：正常流式交互的单帧 CPU、DOM mutation、terminal write 和网络 payload 必须受“新增 delta + viewport + 小幅 overscan”约束，不得与累计活动全文或完整历史线性绑定。**

## 2. 与原方案的关系

### 2.1 原方案保持不变

本增补不替代、不复制、不修改 `docs/design/tui-long-session-performance.md`。原方案中的以下工作仍需按原顺序实施：

- OutputTree root tail append、subtree tail splice 和 node range；
- transcript 每 turn 增量追加、幂等事务和旧会话压实；
- UI event bus batch yield，以及在累计快照契约成立后的安全 coalescing。

### 2.2 依赖关系

| 本增补任务 | 对原方案的依赖 |
|---|---|
| TUI viewport-first Rich render | 可独立实施；完成原方案 OutputTree range API 后更简单 |
| TUI 活动 stream 增量 Markdown | 可独立实施；必须保持原方案增量 tree cache 等价性 |
| Desktop rAF 批处理和增量 Markdown | 可独立实施 |
| `workspace.patch` 和 stream append delta | 与原方案 event contract/coalescing 协同实施 |
| transcript 分页、live tree eviction | 依赖原方案 transcript delta 已 durable |
| 旧会话压实 | 完全沿用原方案，不在本文重复设计 |

### 2.3 本增补明确不重复的内容

- 不重新设计 `_dirty` / `_dirty_nodes`；
- 不重新设计 transcript JSONL transaction；
- 不重新设计 `UiEventBus` barrier/coalescing；
- 不把 WebView 问题误归因于 Tauri Rust shell；当前证据显示桌面热路径主要位于共享 Gateway 和 `frontend/`。
### 2.4 本轮实施记录（2026-08-26）

本轮只实施并验证 TUI viewport-first 有界 Rich 转换，本文档**仍为部分实施，未整体闭环，不归档**：

- [x] 活动 transcript 与 prompt/panel 热路径在最终 viewport 裁剪前使用尾部 bounded helper；默认 overscan 为 `min(max(row_limit, 8), 32)`，保留可见尾部、markup 异常安全回退和输入原文状态。
- [x] 复杂度回归覆盖 10,000 条活动逻辑行/1-row viewport，以及 panel 调用链；`text_from_line()` 调用量受 viewport + overscan 约束。
- 验证：`./test.py --backend -- tui/tests/test_frame_advanced.py -v`（20 passed）；相关集合 `./test.py --backend -- src/tests/test_presentation/gateway src/tests/test_presentation/output tui/tests`（754 passed，含 `tui/tests/test_output_tree.py` 17 passed）。

仍未完成：活动 Markdown bounded projection/canonical worker、Desktop rAF/Markdown worker/keyed reconciliation/DOM window、capability/workspace patch/transcript window、terminal writer/backpressure、输入/paste/candidate 优化及 durable live-history eviction。

### 2.5 本轮实施记录（2026-08-28）

本轮只闭环 TUI canonical commit 的异步本地路径和退出顺序；Desktop Worker、协议窗口化和慢 terminal writer 仍未实现，本文档保持 `in-progress/partial`：

- [x] **TUI 异步 canonical commit**：`DockEventConsumer` 对 `AssistantStreamCommitted` 立即创建任务，canonical projection 使用 `asyncio.to_thread()`；`BottomInputDock` 以 node/revision/generation 三重校验安装结果，stale 结果丢弃，worker 异常回退 escaped plain projection。
- [x] **TUI scrollback/shutdown 安全路径（不包含慢 PTY/backpressure）**：`render_pending` 阻止 provisional stream 进入 native scrollback；commit drain 位于 event bus stop 之前；退出时先 force flush 和 writer flush，再 restore terminal、写退出序列，最后在线程中导出 `transcript.log`。
- 验证：目标 backend 测试（75 passed）；presentation + TUI 集合（779 passed）；`./test.py --frontend`（677 passed）。完整 backend 为 5163 passed、2 failed、30 skipped，失败为两个 runtime session/todo 测试；当前没有独立基线可证明它们是既有问题，且未在本轮目标路径中解决。

仍未完成：bounded StreamingMarkdownProjection、Desktop Markdown Worker/rAF/keyed reconciliation/DOM window、capability/workspace patch/transcript window、TerminalWriter 慢 PTY/backpressure、输入/paste/candidate 优化及 durable live-history eviction。

### 2.6 本轮实施记录（2026-08-29）

本轮闭环 **P0.6 TerminalWriter 慢 PTY/backpressure**；本文档其他 P0/P1/P2 项仍未整体完成，因此继续保持 `in-progress/partial`：

- [x] 单独 worker thread 成为 TTY stdout frame/scrollback 单一 owner；frame 只合并连续 pending generation，commit/barrier 保序且不可丢弃。
- [x] worker 以最后实际写入 frame 为 diff baseline；commit aggregate 超过 4 MiB 时显式 rollover 到 `SpooledTemporaryFile`，成功、失败和 shutdown 均回收 payload。
- [x] frame/render/state/app/parser 完成 worker 集成；Windows input 改为可取消的 `kbhit()` 10 ms polling。
- [x] startup、writer failure、final commit、termios restore、restore barrier、shutdown 与 transcript export 按统一生命周期清理；外部取消在 cleanup/reap 完成后重新传播。
- 验证：慢 PTY heartbeat（1 passed）；规格聚焦集合（238 passed）；完整 TUI（397 passed）；完整 backend（5235 passed、30 skipped）；`py_compile`、目标 `git diff --check`、相关 LSP diagnostics 与直接终审均通过。

仍未完成：bounded StreamingMarkdownProjection、Desktop Markdown Worker/rAF/keyed reconciliation/DOM window、capability/workspace patch/transcript window、输入/paste/candidate 其余优化及 durable live-history eviction。

## 3. 已验证证据


### 3.1 审计范围

只读审计覆盖：

- TUI：`tui/voidx_cli/`、`src/voidx/presentation/output/dock/`；
- Desktop/WebView：`frontend/src/`；
- Gateway/protocol：`src/voidx/presentation/gateway/`、`src/voidx/presentation/protocol/`；
- Native shell：`desktop/tauri/src/main.rs`。

基准环境为 2026-08-14、macOS arm64、当前工作区源码。全部使用合成文本/节点，不读取真实用户 session。

### 3.2 单个活动 stream 形成累计全文 O(n²)

TUI 当前路径：

```text
AssistantStreamUpdated(text=累计全文)
  -> BottomInputDock.set_stream
  -> DockStreamMixin._update_stream_node
  -> _clean(累计全文)
  -> _markdown_lines(累计全文)
  -> 替换完整 header/body_lines
  -> OutputTree render
  -> Rich/ANSI frame render
```

关键实现：

- `src/voidx/presentation/output/dock/stream.py:_update_stream_node()` 每次对累计全文调用 `_markdown_lines()`；
- `tui/voidx_cli/render_frame.py:_transcript_elements_for_rows()` 再执行 Rich/ANSI 转换。

Desktop 当前路径：

```text
item.delta(data.text=累计全文)
  -> appendStreamText
  -> 100ms debounce
  -> renderStreamText
  -> marked.parse(累计全文)
  -> DOMPurify.sanitize(完整 HTML)
  -> highlight.js 扫描所有 code block
  -> replaceChildren(完整 Markdown DOM)
```

关键实现：

- `frontend/src/utils/stream.ts:renderStreamText()`；
- `frontend/src/utils/markdown.ts:renderMarkdown()`。

合成基准：

| 端 | 更新次数 | 最终字符 | 累计耗时 | 最后一次更新 |
|---|---:|---:|---:|---:|
| TUI | 100 | 6,200 | 约 2.1 s | 约 41 ms |
| TUI | 200 | 12,400 | 约 8.2 s | 约 80 ms |
| TUI | 400 | 24,800 | 约 33.3 s | 约 168 ms |
| Desktop/JSDOM | 100 | 9,400 | 约 0.59 s | 约 14 ms |
| Desktop/JSDOM | 200 | 18,800 | 约 2.33 s | 约 22 ms |
| Desktop/JSDOM | 400 | 37,600 | 约 10.52 s | 约 49 ms |

结果随累计文本近似超线性增长。原方案只优化 OutputTree 历史前缀，不能消除 `_markdown_lines()`、`marked`、DOMPurify 和 highlight.js 对当前全文的重复工作。

### 3.3 Gateway 全量 snapshot 跨三层放大

当前 `workspace.snapshot` 路径：

```text
GatewaySession.broadcast_snapshot
  -> _build_workspace_snapshot
  -> _active_thread_snapshot
  -> tree_to_snapshot(完整 OutputTree)
  -> Pydantic model_dump/model_dump_json
  -> WebSocket/Web Worker structured clone
  -> main-thread JSON.parse
  -> renderSidebar(完整 threads)
  -> renderTranscript
  -> root.replaceChildren()
  -> 对每个 node 重建 Markdown/tool/thought DOM
```

关键实现：

- `src/voidx/presentation/gateway/session/core.py:_encode_snapshot()`、`_build_workspace_snapshot()`、`_active_thread_snapshot()`；
- `src/voidx/presentation/protocol/transcript.py:tree_to_snapshot()`；
- `frontend/src/main.ts:renderWorkspaceSnapshot()`；
- `frontend/src/utils/render.ts:renderTranscript()`。

Gateway 在以下场景发送 snapshot：

- 客户端连接；
- thread switch；
- submit 前后部分状态变化；
- turn completed/failed/cancelled；
- `refresh.requested`。

10k 合成节点，仅 Python 端 DTO + JSON 的中位数：

| 节点数 | DTO | JSON | 合计 | payload |
|---:|---:|---:|---:|---:|
| 1,000 | 约 2.1 ms | 约 1.0 ms | 约 3.1 ms | 约 0.66 MB |
| 5,000 | 约 10.9 ms | 约 5.2 ms | 约 16.2 ms | 约 3.33 MB |
| 10,000 | 约 35.0 ms | 约 10.9 ms | 约 47.5 ms | 约 6.67 MB |

这尚未包含 WebSocket 复制、Worker → main thread 消息复制、JSON.parse、Markdown 和 DOM 重建。

### 3.4 TUI 在裁剪前完成全量 Rich 渲染

`render_frame.py:_render_impl()` 虽然最终只显示 terminal height 内的行，但当前顺序是：

1. 取得全部 active lines；
2. 每行调用 `text_from_line()`；
3. 将完整 Group capture 为 ANSI；
4. 最后执行 `ansi.splitlines()[-row_limit:]`。

因此 10k 条 active lines、`row_limit=1` 仍会做 10k 次 Rich 转换。大型 stream、tool result、clarify/permission prompt 都受影响。

### 3.5 TUI stdout 与 scrollback flush 同步阻塞事件循环

同步调用点：

- `tui/voidx_cli/app.py:_flush_committed()`；
- `tui/voidx_cli/render_frame.py:_render_full()`、`_render_diff()`、`_render_input_region()`、`_render_busy_activity_tick()`。

当前流程使用 `sys.stdout.write()` / `Console.print()` / `sys.stdout.flush()`，没有字节预算或独立 writer。慢 SSH、PTY 或 terminal backpressure 会直接阻塞 asyncio stdin reader。即使原方案把 flush 限制到“新增后缀”，单次新增 tool output 仍可能达到 MB 级。

### 3.6 TUI 单帧重复生成同一布局

一次 `_render_frame()` 内会重复生成或 capture：

- bottom/status/panel；
- thinking stream；
- busy activity；
- input/cursor rows。

这会放大大型 prompt、thinking stream 和输入草稿的成本。旧方案只优化 thinking line map 查询，没有保证“同一 frame 只格式化一次”。

### 3.7 输入与本地 I/O 仍有同步超线性路径

已确认：

- `tui/voidx_cli/parser.py:_process_input()` 对单次 read 的普通字节逐 key dispatch；
- `tui/voidx_cli/input.py` 每个字符切片重建当前行并刷新 panel；
- bracketed paste 使用不可变 `bytes += data`；
- `list_file_candidates()` 在按键路径同步 `scandir + stat + sort` 后才截取 8 项；
- skill/MCP 候选也会在 query 每变化一个字符后重新枚举；
- TUI 退出前在 terminal restore 之前同步 render 全树并覆盖 `.voidx/transcript.log`。

这些成本主要随活动草稿、目录大小或完整历史增长，不与 OutputTree tail append 重复。

### 3.8 Native Tauri shell 结论

`desktop/tauri/src/main.rs` 负责 backend spawn、bootstrap stderr 读取和 WebView 生命周期。聊天数据通过 WebSocket 直接进入 `frontend/`，Rust shell不参与逐条 stream/DOM 渲染。因此：

- 本轮没有证据支持把 Tauri Rust shell列为长会话 P0 根因；
- P0/P1 默认不修改 native shell；
- 若后续 instrumentation 发现 backend restart、stderr 或 window IPC 长任务，再单独立项，不在本增补预先重构。

## 4. 性能模型

定义：

- `H`：完整历史节点/行数；
- `A`：当前活动 stream/prompt/input 的累计字符或行数；
- `Δ`：本次新增文本；
- `V`：viewport 可见行/turn 数；
- `D`：已挂载 DOM 节点数；
- `S`：workspace session 数；
- `W`：本次 terminal/network 写入字节；
- `M`：有界 mutable tail，首版 hard limit 为 16 KiB。

目标复杂度：

| 操作 | 当前上界 | 目标上界 |
|---|---|---|
| TUI stream update | `O(A + H)`，原方案后仍可能 `O(A)` | `O(Δ + M + V)` |
| Desktop stream update | `O(A + code_blocks + layout(D))` | `O(Δ + M + visible_layout)` |
| canonical commit | 事件循环/主线程同步 `O(A)` | worker 中 `O(A)`；事件循环/主线程每次安装 `O(V)` 且受 8 ms 预算约束 |
| 正常 turn 完成 | `O(H + S)` snapshot + `O(D)` DOM rebuild | `O(metadata_patch + new_items)` |
| 连接/切换 thread | `O(H)` 全 snapshot | `O(recent_window)`，旧页按需加载 |
| TUI frame | `O(active_lines + panel_payload)` | `O(V + overscan)` |
| TUI terminal output | 同步 `O(W)` 阻塞事件循环 | writer thread 中 `O(W)`；主循环 enqueue 受预算约束 |
| 批量输入/paste | 可达 `O(A²)` copy | `O(A + Δ)` |
| 端侧常驻内存 | `O(H + D)` | TUI `O(retained_turns)`；Desktop `O(windowed_turns)` |

## 5. 目标与非目标

### 5.1 目标

1. 50k 字符单个 assistant response 在 TUI 和 Desktop 中保持可交互；
2. 正常 turn 完成不再跨 Gateway 发送完整 transcript；
3. Desktop DOM 挂载量由 viewport/window 决定，而非完整会话节点数；
4. TUI 可见 frame 的 Rich 工作量由 terminal height + overscan 决定；
5. 慢 terminal writer 不再阻塞 stdin、cancel 和 resize 任务；
6. protocol revision 失配、非 append stream update 和 reconnect 均可恢复为正确最终文本；
7. 保持旧客户端可用，支持分阶段启用新能力。

### 5.2 非目标

- 不在本文重新设计 transcript JSONL；
- 不将 Markdown 改为纯文本；
- 不关闭代码高亮、DOMPurify 或安全净化；
- 不丢弃已 committed terminal 输出或语义 UI event；
- 不要求连接/切换 100k 节点会话完全零成本，但要求分页并避免单次主线程 long task；
- 不预先重写 Tauri backend lifecycle；
- 不通过把 debounce 从 100ms 粗暴提高到数秒掩盖单帧成本。

## 6. 必须保持的不变量

### 6.1 Stream

- 每个 stream 有稳定 `(thread_id, turn_id, item_id)`；
- revision 单调递增；
- append delta 只有在 `base_revision` 等于客户端当前 revision 时可应用；
- 非前缀更新、phase 切换、retry replacement 必须使用 replace 或 snapshot fallback；
- commit 后 Desktop/TUI 最终可见文本必须逐字符等于 canonical full text；
- thinking 与 text 不共享 mutable tail，不能跨 phase 拼接。

### 6.2 Snapshot 与 patch

- `workspace.snapshot` 仍是 canonical recovery payload；
- `workspace.patch` 只能更新 thread/runtime metadata，不能隐式删除 transcript item；
- patch 带 workspace revision；缺 revision 或 gap 时客户端请求 snapshot；
- 旧客户端未声明能力时继续接收现有 full-replace stream 和 full snapshot；
- 新旧 client 可同时连接同一 GatewaySession，server 必须按 client capability 编码或安全降级到旧格式。

### 6.3 Desktop DOM

- keyed reconciliation 以 thread/turn/item id 为键，不以展示文本为键；
- 不得在 append-only item 到达时重建已 committed turn DOM；
- 用户离开底部查看历史时，不得被 stream update 强制拉回底部；
- 卸载旧 turn DOM 不得丢失 canonical transcript，重新进入 window 时可从 snapshot page 恢复；
- commit 时执行一次 canonical full Markdown render，用于修正增量预览边界差异。

### 6.4 TUI terminal

- terminal writer 是 stdout frame/scrollback 的单一所有者；
- frame batch 可被更新 frame 合并，但 committed scrollback、clear/reset barrier 和 terminal restore sequence 不可丢弃或重排；
- input state 更新不等待 terminal 实际 drain；
- shutdown 必须按顺序 drain 必要 barrier、恢复 terminal，再执行可延迟导出；
- writer 异常必须回报主循环并进入可恢复的 plain-output fallback。

### 6.5 Live history eviction

只有同时满足以下条件的 root turn 才可从 live OutputTree/DOM 驱逐：

1. turn 已 terminal completed/failed/cancelled；
2. transcript durable watermark 已覆盖该 turn；
3. TUI 对应行已 committed 到 native scrollback，或 Desktop canonical page 可从 persistence 读取；
4. 没有 permission/checkpoint/tool/agent 等运行时引用；
5. 当前不是用户展开、搜索或 diff review 的目标 turn。

## 7. 设计方案

### 7.1 P0-A：TUI viewport-first render

#### 7.1.1 Active transcript

将 `_transcript_elements_for_rows()` 改为 visible-row-first：

1. 从 active logical lines 尾部向前取候选；
2. 逐逻辑行转换 Rich，并累计其视觉行数；
3. 达到 `row_limit + overscan_rows` 后停止；
4. 只 capture 候选 Group；
5. 最后裁到精确 `row_limit`。

默认 `overscan_rows = min(max(row_limit, 8), 32)`。逻辑行是独立 markup line，因此从尾部截取不会继承前行 Rich style。异常 markup 继续回退 `Text(line)`。

复杂度测试必须 spy `text_from_line()` 调用次数，而不仅断言最终显示行数。

#### 7.1.2 Prompt/panel

choice、permission、attachment、skill 和 MCP panel 使用同一 bounded visual-row helper。大型 prompt 只转换 viewport 覆盖范围；完整原始文本仍保存在 request state 中，不因展示裁剪而截断提交值。

### 7.2 P0-B：活动 Markdown 增量投影

TUI 与 Desktop 各自实现端侧 `StreamingMarkdownProjection`，共享以下状态机语义：

```text
raw_text
revision
stable_blocks[]
provisional_text_chunks[]
mutable_tail
phase
committed
```

更新规则：

1. 若新文本以旧文本为前缀，只把 `Δ` 加入 mutable tail；
2. parser 识别已闭合且后续 append 不会改变的 block，将其移入 `stable_blocks`；
3. 只重新 parse mutable tail；
4. 非前缀 replacement、phase change 或 parser 不确定时，回退 full projection；
5. commit 生成 canonical full render，并与原始文本做逐字符等价断言。

第一版只冻结 parser 能证明**不依赖后续块**的内容：

- 已闭合 fenced code block；
- 不含 reference-style link、HTML 或其他跨块语义的普通 paragraph；
- parser 明确结束、且不含跨块引用的 heading/thematic break；
- parser 明确结束、且后续同级 append 不会继续归属其中的 list/blockquote block。

未闭合 code fence、list continuation、blockquote continuation、HTML block，以及包含 reference-style link/definition 等跨块依赖的内容全部保留在 mutable tail。仅出现空行不能单独证明 block 永久稳定。不要自行用脆弱正则重新实现完整 Markdown grammar；Desktop 使用 `marked.lexer()` token boundary 并叠加上述保守条件，TUI 在 `dock/formatting.py` 上增加等价 block projection。

#### 7.2.1 有界 provisional preview

只保留 mutable tail 仍不足以覆盖单个无限增长的未闭合 paragraph、code fence 或 HTML block。为保证更新成本有界：

- mutable tail soft limit 为 8 KiB，hard limit 为 16 KiB；
- 超过 hard limit 时，在 Unicode code point 和逻辑行安全边界上，将最旧前缀移动到 `provisional_text_chunks`，只保留最近 8 KiB；
- provisional chunk 必须使用 `textContent` / Rich escape 作为纯文本追加，不能作为 HTML 或 Rich markup 解释；
- provisional chunk 只保证流式预览的**文字内容与顺序**，不承诺最终 Markdown 样式；
- canonical `raw_text` 始终完整保存，不能用 provisional projection 反向构造协议、snapshot 或 persistence 数据；
- Desktop 已追加 provisional DOM 不再重复解析；TUI 已生成的 provisional visual rows 只追加，frame 仍按 viewport-first 读取；
- non-prefix replace 清空投影并按新 canonical text 重建，不能把旧 provisional chunk 留在画面中。

因此普通 append 的工作量受 `Δ + 16 KiB tail + viewport` 限制。阈值是首版内部常量，benchmark 达标后再调整。

#### 7.2.2 Canonical commit

commit 必须修正 provisional preview 可能存在的临时样式差异，但不能把 50k+ 文本的 full parse 重新放回事件循环或浏览器主线程。

TUI 的异步所有权明确放在现有 `DockEventConsumer`，但后台 parse **不得阻塞 `UiEventBus`**：

1. `BottomInputDock.prepare_stream_commit()` 返回不可变 `(node_id, revision, raw_text, phase)`，将节点从 active stream 脱离并标记 `render_pending + unsettled`；provisional preview 保留；
2. `DockEventConsumer.handle(AssistantStreamCommitted)` 用 `asyncio.create_task()` 调度 `_finish_stream_commit(work_item)` 后立即返回，因此 Gateway mirror 和后续 UI event 不等待本地 Markdown parse；
3. `_finish_stream_commit()` 内部调用 `await asyncio.to_thread(build_canonical_stream_lines, work_item)`，返回后调用 `BottomInputDock.apply_stream_commit(work_item, lines)`；只有 node/revision 仍匹配才原子替换 projection、清除 `render_pending`、mark settled 并 refresh；
4. 后续 tool/turn 事件可以继续追加节点，但 `safe_flush_line_count()` 在最早的 `render_pending` 节点处停止，不能把 provisional 内容写入不可修改的 native scrollback；
5. 同一节点的 discard/reset/replace 使 work item 失效；后续新 stream 使用新 node/revision，不取消前一个已提交节点的 canonical task；
6. `DockEventConsumer` 跟踪所有 commit tasks；capture stop/shutdown 先等待限定时间，超时则用 escaped canonical plain text settle，再交给 terminal writer drain；
7. 无异步 event consumer 的 legacy/plain fallback 保留同步 `commit_stream()`，但不属于 TUI 性能路径，也不得被 TUI bootstrap 选中。

Desktop 在专用 `frontend/src/utils/markdown.worker.ts` 中执行 lex/parse 和代码高亮，返回按 block 分段的渲染描述；worker 不直接产生可信 HTML。主线程对所有 HTML 片段执行 DOMPurify，并按每帧最多 8 ms 的 mutation budget 安装 canonical DOM。单个超大 raw HTML block 无法在预算内净化时，使用 escaped plain-text canonical fallback，并记录 `html_block_budget` 原因；安全优先于保留 HTML 样式。

共同规则：

- worker 结果携带 `(item_id/node_id, revision)`；过期结果丢弃；
- canonical install 完成前保留 provisional preview，不显示空白或重复文本；
- commit 完成后，最终 textContent 必须逐字符等于 canonical raw text，正常异步路径的最终 DOM/ANSI 必须与一次性 full render 等价；
- 后台任务失败时保留 escaped plain-text canonical output，并记录 fallback，不得丢正文；
- turn terminal event、transcript durable watermark 和 Desktop item completed 通知可以先推进业务状态，但对应 stream 在 canonical install 完成前保持 `render_pending`，不得被 live-history eviction 卸载。

这一区分“增量帧延迟”和“canonical commit 端到端时间”：前者必须在交互帧预算内，后者可以跨多个帧完成，但不能产生事件循环或主线程 long task。

### 7.3 P0-C：Desktop 每帧批处理与布局隔离

`frontend/src/utils/stream.ts` 调整：

- 100ms timer 仅负责吞吐上限，不直接执行多次 DOM render；
- 同一 stream 在一个 `requestAnimationFrame` 中最多 mutate DOM 一次；
- stable block DOM 只 append 一次，mutable tail 使用单独容器 replace；
- highlight.js 只处理新闭合 code block；active 未闭合 code fence 可暂时使用 escaped `<pre>`，commit 时高亮；
- DOMPurify 只净化新 block/尾块，不重复净化 stable blocks。

滚动规则：

1. mutation 前只读取一次 `isNearBottom`；
2. mutation 后仅当此前 near-bottom 时写一次 scroll position；
3. 所有 layout read 在 DOM write 前，避免 read/write 交错；
4. 用户向上滚动后显示“回到底部”提示，不自动抢滚动位置。

### 7.4 P0-D：TUI 单帧 RenderPlan

新增内部不可变 `RenderPlan`，一次采集：

- width/height；
- transcript visible elements；
- thinking elements；
- busy activity elements；
- todo/panel/status/input elements；
- 各 region row count；
- cursor target；
- 最终 ANSI lines。

`_render_frame()`、bottom diff、busy layout 和 cursor positioning 只能读取同一 plan，不得再次调用 panel/status/thinking renderer。busy-only tick 可创建只包含 busy region 的小 plan，但必须复用上一 full plan 的 geometry。

### 7.5 P0-E：TerminalOutputWriter

将同步 stdout 写入重构为有序 batch：

```text
FrameBatch(generation, start_row, target_lines)  # 自包含目标帧，可合并
CommitBatch(sequence, ansi)                      # 不可丢弃
BarrierBatch(kind=clear|restore)                 # 不可丢弃、不可越过
```

实现要求：

- 单独 worker thread 拥有 stdout write/flush；
- asyncio 主循环只构建自包含的目标 frame 或 commit batch 并 enqueue，不执行可能阻塞的 terminal write；
- frame queue 只保留最新 generation；worker 以“最后实际写入 terminal 的 target lines”为 diff 基线，不能使用可能已被合并丢弃的中间帧；
- `CommitBatch`、clear、resize、scroll 和 restore 会使 writer 的 frame 基线失效，下一帧必须 full render；
- commit batch 保序；若内存字节超过 soft limit，spool 到 `SpooledTemporaryFile`，不丢输出；
- barrier 等待此前 commit 完成；
- writer 将错误通过 `loop.call_soon_threadsafe()` 返回；
- 测试覆盖跳过多个 generation 后的画面等价、慢 PTY、partial write、EPIPE、shutdown 和 restore 顺序。

第一版 soft limit 建议 4 MB，frame batch 不计入 committed spool。该值是配置常量并记录指标，不暴露用户设置。

### 7.6 P1-A：Capability negotiation

当前 `GatewaySession.connect()` 会在 WebSocket 建立后立即发送第一个 snapshot；如果等到 snapshot 之后才声明能力，首屏仍会传输完整历史。因此能力协商分两步：

1. Frontend 在建立 WebSocket 前，用 `URL.searchParams` 把能力预声明追加到现有 gateway URL，保留原有鉴权参数：

   ```ts
   const url = new URL(gatewayUrl);
   url.searchParams.set(
     "cap",
     "stream_append_v1,workspace_patch_v1,transcript_window_v1",
   );
   ```

2. `GatewayServer._handle()` 在调用 `GatewaySession.connect(client)` 前解析 `cap`，写入该 `_WebSocketClient`；首个 snapshot 已可按 `transcript_window_v1` 返回 recent window。
3. socket open 后，Frontend 再调用 `client.capabilities` RPC 确认同一集合并取得 server 接受的交集：

   ```json
   {
     "capabilities": {
       "stream_append_v1": true,
       "workspace_patch_v1": true,
       "transcript_window_v1": true
     }
   }
   ```

URL 中的 capability 只是格式/性能协商，不是授权信息；服务端只接受 allowlist 中的已知值。未预声明、RPC method-not-found、未知字段或 URL/RPC 不一致时，均取安全交集并降级 legacy 行为。

Gateway 为每个 `ProtocolClient` 保存 capability，不使用 session 全局布尔值。`connect()`、`_broadcast()` 和 snapshot encoder 按 capability 分组；同一事件最多生成 legacy 与新协议两种编码，不按客户端重复遍历 OutputTree 或重复构造相同 JSON。

### 7.7 P1-B：Stream append delta

Gateway adapter 已缓存上一次 full text。对支持 `stream_append_v1` 的 client，发送：

```json
{
  "item_id": "...",
  "kind": "assistant_stream",
  "lifecycle": "delta",
  "data": {
    "op": "append",
    "base_revision": 12,
    "revision": 13,
    "text": "new suffix",
    "phase": "text"
  }
}
```

若 `new_text.startswith(old_text)` 不成立，发送：

```json
{
  "data": {
    "op": "replace",
    "revision": 14,
    "text": "canonical full text",
    "phase": "text"
  }
}
```

commit 必须携带最终 revision、字符数和可选 hash。客户端 revision 不匹配时不猜测拼接，调用 `session.snapshot` 获取当前 thread 的 canonical recent window。

旧 client 继续收到当前 `data.text = complete text` 的 `item.delta`。

### 7.8 P1-C：Workspace metadata patch

新增 `workspace.patch`，只承载：

- workspace revision；
- active thread id；
- thread upsert/remove；
- runtime/provider/model/permission/write-lock patch。

正常 submit staging、turn started/completed/failed/cancelled、title/status/message_count 更新只发送 patch。以下场景继续发送 snapshot：

- 首次连接；
- thread switch；
- client 主动请求 recovery；
- revision gap；
- reset/clear 等 canonical replacement。

`refresh.requested` 不再无条件导致 full transcript snapshot：

- 只变更 metadata 时发送 patch；
- transcript revision 未变时不发送 transcript；
- 明确要求 canonical refresh 时发送 recent window snapshot。

### 7.9 P1-D：Transcript window 与 Desktop reconciliation

`ThreadSnapshot` 增加兼容可选字段：

```text
window_revision
before_cursor
has_more_before
complete
```

新增 `session.transcriptPage(thread_id, before_cursor, turn_limit)` RPC。默认：

- 初始/切换 snapshot 返回最近 40 个 root turns；
- 向上滚动接近顶部时加载前 40 turns；
- cursor 是服务端不透明字符串，client 不解析；
- recovery 可请求 `complete=false` recent canonical window，不需要传输全部历史。

Frontend 将 root turn 渲染为 keyed section：

```text
thread transcript
  -> turn section[data-turn-id]
     -> item[data-item-id]
```

规则：

- snapshot window 与现有 DOM 按 id reconcile；
- unchanged committed turn 不重建；
- prepend older page 时用 scroll anchor 保持视口；
- 默认只挂载 viewport 附近 turn + 10 turn overscan；
- 被卸载 section 只保留轻量 height placeholder 和 canonical node model；
- 搜索命中、diff review 或用户展开节点时 pin 对应 turn，避免虚拟化卸载。

### 7.10 P2-A：TUI 输入与候选查询

#### 批量输入

`_process_input()` 识别连续 printable UTF-8 run，在不包含 escape/control/paste marker 时一次调用 `_insert_text_run()`：

- 当前行只切片一次；
- cursor 更新一次；
- panel 更新一次；
- render invalidation 一次。

#### Paste

`_paste_buffer` 改为 `bytearray`，分片用 `extend()`；结束时一次 `decode()`。设置最大内存阈值，超过阈值转 `SpooledTemporaryFile`，避免 100MB paste 常驻双份 bytes。

#### 候选查询

- file candidate 迁移到异步 query worker；
- query 带 generation，过期结果丢弃；
- 目录缓存按 `(workspace, dir, mtime/invalidation generation)`；
- 先使用 prefix heap/partial selection，不对全量结果排序后再截 8 项；
- skill/MCP catalog 在 workspace/config revision 变化时刷新，不在每个按键重新枚举。

### 7.11 P2-B：退出导出

`transcript.log` 是兼容调试副产物，不是 durable source。退出顺序改为：

1. 停止新 frame；
2. terminal writer drain 必要 commit/barrier；
3. 恢复 cursor、paste mode 和 termios；
4. 再通过 `asyncio.to_thread()` 导出 `transcript.log`；
5. 导出超时或失败只记录内部错误，不阻止 terminal 恢复和进程退出。

后续可将该文件改为显式命令按需生成，但本阶段保留兼容行为。

### 7.12 P2-C：Bounded live history

依赖原方案 transcript durable watermark。新增 live retention policy：

- TUI 默认保留最近 20 个 root turns 或 16 MB projected body，以先到者为准；
- Desktop canonical model 默认保留已加载 window，DOM 只挂载 viewport + overscan；
- 达到阈值时按第 6.5 节条件驱逐最旧 turn；
- Gateway snapshot/page 从 persistence projection 读取已驱逐 turn，不依赖 live OutputTree 永久保存全部历史。

阈值先作为内部常量；通过 metrics 验证后再决定是否开放配置。

### 7.13 可观测性

新增或扩展指标：

| 指标 | 含义 |
|---|---|
| `stream_raw_chars` | 当前累计 stream 字符 |
| `stream_delta_chars` | 本次新增字符 |
| `stream_mutable_tail_chars` | 本次重解析尾块大小，必须 ≤ 16 KiB |
| `stream_provisional_chars` | 已按 escaped plain text 冻结的预览字符 |
| `stream_update_ms` | 普通增量帧投影耗时 |
| `stream_commit_worker_ms` | canonical worker parse/highlight 耗时 |
| `stream_commit_install_ms` | canonical 结果端侧安装总耗时 |
| `stream_commit_pending` | 等待 canonical settle 的 stream 数 |
| `stream_projection_fallback_reason` | non-prefix / phase / parser-uncertain / html-block-budget / worker-error |
| `snapshot_nodes/bytes/build_ms` | Gateway snapshot 构造与大小 |
| `workspace_patch_bytes` | metadata patch 大小 |
| `frontend_long_task_ms` | Desktop 主线程长任务 |
| `mounted_turns/dom_nodes` | Desktop 挂载窗口规模 |
| `tui_visible_rows/converted_lines` | TUI viewport 与实际 Rich 转换量 |
| `terminal_queue_bytes` | writer 待写字节 |
| `terminal_oldest_batch_ms` | 最旧 batch 等待时间 |
| `terminal_spooled_bytes` | backpressure spool 大小 |
| `input_batch_chars` | 一次批量插入字符数 |
| `candidate_query_ms/stale` | 候选查询耗时和过期结果数 |

慢日志 rate-limit；正常 stream 不逐 delta 写日志。

## 8. 文件改动范围

### 8.1 Shared Gateway/protocol

| 文件 | 责任 |
|---|---|
| `src/voidx/presentation/gateway/session/core.py` | per-client capability、workspace patch、snapshot/page/recovery 路由 |
| `src/voidx/presentation/gateway/session/consumer.py` | refresh 按 metadata/transcript revision 决定 patch 或 snapshot |
| `src/voidx/presentation/gateway/server.py` | patch/delta 的队列替换和 backpressure 分类 |
| `src/voidx/presentation/gateway/adapter.py` | full text → append/replace projection、stream revision |
| `src/voidx/presentation/protocol/v2/snapshot.py` | window metadata 和 workspace patch DTO |
| `src/voidx/presentation/protocol/v2/threads.py` | capability/patch 或 item revision DTO（按最终所有权放置） |
| `src/voidx/presentation/protocol/transcript.py` | 按 root turn 导出 snapshot window |
| `scripts/export_ui_protocol_schema.py` | 导出新增 RPC/schema |
| `src/tests/test_presentation/gateway/test_gateway_v2_session.py` | snapshot 频率、patch、capability、recovery |
| `src/tests/test_presentation/gateway/test_gateway_v2_routing.py` | 新 RPC routing |
| `src/tests/test_presentation/protocol/test_dto.py` | window/patch/revision DTO contract |

### 8.2 Desktop/WebView

| 文件 | 责任 |
|---|---|
| `frontend/src/main.ts` | capability 注册、patch/recovery/page 处理，取消正常 turn 全量 render |
| `frontend/src/rpc/client.ts` | capability 和 recovery RPC；保持 JSON parsing 单入口 |
| `frontend/src/utils/stream.ts` | rAF batch、revision、stable/provisional/mutable projection 和 canonical install |
| `frontend/src/utils/markdown.ts` | block projection、主线程 DOMPurify 和 escaped fallback |
| `frontend/src/utils/markdown.worker.ts`（新建） | 后台 canonical lex/parse/highlight，返回不可信的分块渲染描述 |
| `frontend/src/utils/render.ts` | keyed turn/item reconciliation，不再全量 `replaceChildren()` |
| `frontend/src/ui/sidebar.ts` | thread patch keyed 更新，避免 snapshot 时全量重绘 |
| `frontend/src/ui/transcript-window.ts`（新建） | turn window、placeholder、scroll anchor 和 pin policy |
| `frontend/test/utils/stream.test.ts` | append/replace/revision/provisional/commit 等价 |
| `frontend/test/utils/markdown-worker.test.ts`（新建） | worker revision、过期结果、净化边界和 worker fallback |
| `frontend/test/utils/render.test.ts` | keyed reconciliation 和 unchanged DOM identity |
| `frontend/test/ui/sidebar.test.ts` | metadata patch 只更新目标 thread |
| `frontend/test/main/main.test.ts` | capability、snapshot gap、page recovery |
| `frontend/test/performance/cross-ui-performance.test.ts`（新建） | 调用次数、tail 上界、commit frame budget 和 DOM 挂载量回归 |

Native `desktop/tauri/` 当前不在计划改动范围。

### 8.3 TUI

| 文件 | 责任 |
|---|---|
| `tui/voidx_cli/render_frame.py` | viewport-first、RenderPlan、单帧单次格式化 |
| `tui/voidx_cli/terminal_writer.py`（新建） | frame/commit/barrier writer thread 与 spool |
| `tui/voidx_cli/app.py` | writer lifecycle、async shutdown、退出导出顺序 |
| `tui/voidx_cli/parser.py` | printable run 和 bytearray/spooled paste |
| `tui/voidx_cli/input.py` | `_insert_text_run()` |
| `tui/voidx_cli/panels.py` | generation-based async candidate query |
| `src/voidx/presentation/output/dock/stream.py` | bounded StreamingMarkdownProjection、prepare/apply commit 和 revision |
| `src/voidx/presentation/output/dock/formatting.py` | TUI Markdown block projection 与 canonical builder |
| `src/voidx/presentation/output/events/consumers.py` | 后台 canonical commit task、stale guard、shutdown drain/fallback |
| `src/voidx/presentation/tools/file_picker.py` | 可缓存目录 projection 和 partial top-k |
| `src/tests/test_presentation/gateway/test_ui_events_streaming.py` | async commit 不阻塞 event bus、stale result 和最终等价 |
| `src/tests/test_presentation/output/test_scrollback_flush.py` | render_pending 节点阻断 unsafe scrollback flush |
| `tui/tests/test_frame_advanced.py` | 转换量受 viewport 限制、RenderPlan 单次调用 |
| `tui/tests/test_input_advanced.py` | 批量输入复杂度 |
| `tui/tests/test_paste_handling.py` | 分片 paste 线性增长与 spool |
| `tui/tests/test_terminal_panels.py` | 大 prompt、大目录和 stale query |
| `tui/tests/test_terminal_writer.py`（新建） | 慢 PTY、顺序、合并、spool、restore |

### 8.4 Benchmark

| 文件 | 责任 |
|---|---|
| `scripts/benchmark_cross_ui_performance.py`（新建） | 合成 Gateway snapshot、TUI stream/frame/input/PTY 基准 |
| `frontend/test/performance/cross-ui-performance.bench.ts`（新建） | Desktop stream、snapshot reconciliation 和 DOM window 基准 |

## 9. TDD 实施任务

### 9.1 P0：端侧止血

- [x] **P0.1 TUI viewport-first bounded render**：10k active lines、`row_limit=1`，断言 Rich 转换量受 viewport + overscan 约束而不是历史总行数线性增长。
  - 文件：`tui/tests/test_frame_advanced.py`
  - GREEN：最终可见尾部保持一致，panel/choice 等底部路径同样在 Rich 转换前有界裁剪；markup 异常回退为原文 `Text`。
  - 命令：`./test.py --backend -- tui/tests/test_frame_advanced.py -v`（20 passed）

- [ ] **P0.2 bounded stream projection**：覆盖普通 append、50k 单段 paragraph、50k 未闭合 fence、list continuation、phase switch 和 non-prefix replace。
  - 文件：`src/tests/test_presentation/output/test_stream_projection.py`（新建）、`frontend/test/utils/stream.test.ts`
  - RED：当前实现随累计全文重复 parse；单段文本没有 tail 上界。
  - GREEN：mutable tail 始终 ≤ 16 KiB；provisional chunk 全部 escaped；canonical raw text 逐字符不变；replace 清空旧 projection。
  - 命令：
    - `./test.py --backend -- src/tests/test_presentation/output/test_stream_projection.py -v`
    - `./test.py --frontend -- test/utils/stream.test.ts`

- [ ] **P0.3 异步 canonical commit（跨端整体）**：覆盖 TUI task scheduling、Desktop Worker、stale revision、失败 fallback、safe flush 和最终等价。
  - [x] **TUI 子项**：`DockEventConsumer` 立即调度 `asyncio.to_thread()` canonical projection；node/revision/generation stale guard、plain fallback、`render_pending` scrollback barrier 和 commit drain 已实现。
  - [ ] **Desktop 子项**：Markdown Worker、主线程 canonical install、DOMPurify 边界和最终等价测试仍待实现。
  - 文件：TUI `src/voidx/presentation/output/dock/stream.py`、`src/voidx/presentation/output/events/consumers.py`、`src/tests/test_presentation/gateway/test_ui_events_streaming.py`；Desktop 计划文件保持不变。
  - TUI 验证：`./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_streaming.py src/tests/test_presentation/gateway/test_ui_events_dock_bus.py tui/tests/test_input_advanced.py tui/tests/test_terminal_writer.py`（75 passed）。
  - Desktop 验证：`./test.py --frontend -- test/utils/markdown-worker.test.ts test/utils/stream.test.ts`（Worker 子项待实现）。

- [ ] **P0.4 Desktop rAF/layout test**：同一 frame 输入 100 个 update，render/scroll layout 各最多一次；用户离底时 scrollTop 不变。
  - 文件：`frontend/test/utils/stream.test.ts`
  - 命令：`./test.py --frontend -- test/utils/stream.test.ts`

- [ ] **P0.5 TUI RenderPlan test**：spy panel/status/thinking/input renderer，一次 full frame 各执行一次。
  - 文件：`tui/tests/test_frame_advanced.py`
  - 命令：`./test.py --backend -- tui/tests/test_frame_advanced.py -v`

- [x] **P0.6 Terminal writer RED/GREEN**：慢 PTY reader 下连续 frame + commit + restore。
  - RED：当前事件循环 heartbeat 被同步 flush 阻塞。
  - GREEN：跳过中间 generation 后终态画面正确；commit 不丢；restore 最后；heartbeat gap 达标。
  - 文件：`tui/tests/test_terminal_writer.py`
  - 命令：`./test.py --backend -- tui/tests/test_terminal_writer.py -v`

### 9.2 P1：协议与 Desktop window

- [ ] **P1.1 capability fallback**：一个新 client、一个 legacy client 同时连接；分别收到 append 和 full-replace，最终文本一致。
  - 文件：`src/tests/test_presentation/gateway/test_gateway_v2_session.py`
  - 命令：`./test.py --backend -- src/tests/test_presentation/gateway/test_gateway_v2_session.py -v`

- [ ] **P1.2 stream revision**：append prefix、replace、gap、duplicate revision、commit hash。
  - GREEN：gap 不应用 delta，触发一次 recovery；重复 revision 幂等。
  - 命令：backend + `./test.py --frontend -- test/utils/stream.test.ts`

- [ ] **P1.3 workspace patch**：submit 和 turn terminal 只发送 metadata patch，不构造 `tree_to_snapshot()`。
  - RED：spy 现有 turn completed 会构造完整 snapshot。
  - GREEN：支持 patch 的 client 上调用数为 0；legacy client 保持旧行为。
  - 文件：`src/tests/test_presentation/gateway/test_gateway_v2_session.py`
  - 命令：同 P1.1。

- [ ] **P1.4 transcript page/window DTO**：最近 40 turns、opaque cursor、前页、revision gap 和 clear/reset。
  - 文件：`src/tests/test_presentation/protocol/test_dto.py`、gateway routing tests。
  - 命令：`./test.py --backend -- src/tests/test_presentation/protocol/test_dto.py src/tests/test_presentation/gateway/test_gateway_v2_routing.py -v`

- [ ] **P1.5 Desktop keyed reconciliation**：相同 snapshot node 保持 DOM identity；append item 只新增目标 DOM；prepend page 保持 scroll anchor。
  - 文件：`frontend/test/utils/render.test.ts`、`frontend/test/main/main.test.ts`
  - 命令：`./test.py --frontend -- test/utils/render.test.ts test/main/main.test.ts`

- [ ] **P1.6 DOM window**：10k synthetic nodes 只挂载配置 window + overscan，pin turn 不被卸载。
  - 文件：`frontend/test/performance/cross-ui-performance.test.ts`
  - 命令：`./test.py --frontend -- test/performance/cross-ui-performance.test.ts`

- [ ] **P1.7 schema sync**：新增 protocol model 后重新导出，不手改 generated d.ts。
  - 命令：
    - `./python.py scripts/export_ui_protocol_schema.py`
    - `./test.py --backend -- src/tests/test_contracts src/tests/test_presentation/protocol -v`
    - `./test.py --frontend`

### 9.3 P2：输入、退出与 bounded history

- [ ] **P2.1 printable run**：512/1024/2048/4096 字符单 read 只执行一次字符串插入和 panel update。
  - 文件：`tui/tests/test_input_advanced.py`
  - 命令：`./test.py --backend -- tui/tests/test_input_advanced.py -v`

- [ ] **P2.2 paste buffer**：按 4KiB 分片输入 1/2/4/8MB，copy 次数和峰值内存近线性；超过阈值进入 spool。
  - 文件：`tui/tests/test_paste_handling.py`
  - 命令：`./test.py --backend -- tui/tests/test_paste_handling.py -v`

- [ ] **P2.3 candidate generation**：慢 provider 不阻塞输入；旧 generation 结果不覆盖新 query；大目录不全量排序。
  - 文件：`tui/tests/test_terminal_panels.py`
  - 命令：`./test.py --backend -- tui/tests/test_terminal_panels.py -v`

- [ ] **P2.4 exit order（整体）**：50k 合成行下 terminal restore 先于 transcript.log 导出；导出超时仍恢复 terminal。
  - [x] **TUI 顺序子项**：force flush/writer flush 后 restore terminal，再写退出序列并在线程中导出 transcript；顺序回归已覆盖。
  - [ ] **超时子项**：导出超时后的恢复行为和 50k 合成行压力测试仍待补充。
  - 文件：`tui/voidx_cli/app.py`、`tui/tests/test_input_advanced.py`
  - 命令：`./test.py --backend -- tui/tests/test_input_advanced.py -k "run_restores_terminal_before_transcript_export" -v`

- [ ] **P2.5 durable eviction**：只有 durable + committed + unreferenced turn 被驱逐；page restore 与原 tree snapshot 等价。
  - 文件：新增 OutputTree retention tests + transcript adapter tests。
  - 命令：`./test.py --backend -- src/tests/test_presentation tui/tests -v`

## 10. 性能验收标准

### 10.1 CI 复杂度断言

CI 不依赖绝对机器时间，优先断言：

- 10k active lines、10-row viewport：Rich 转换量不超过 viewport + 32-row overscan 对应逻辑行；
- 50k 单段 stream：两端 mutable tail 始终 ≤ 16 KiB，provisional text 顺序与 canonical raw text 一致；
- 100 stream update/一 animation frame：Desktop DOM mutation ≤ 1 次；
- canonical commit 调度后，后续 UI event 可在 worker 完成前被处理；
- `render_pending` 节点进入 native scrollback 的行数为 0；
- 支持 patch 的 client turn completed：`tree_to_snapshot()` 调用数为 0；
- 10k snapshot nodes：Desktop mounted turn/item 数不超过 window + overscan；
- 4096 printable bytes：字符串插入和 panel refresh 各 1 次；
- terminal frame burst：只写最新 frame，所有 commit sequence 连续且终态画面正确；
- transcript eviction：未 durable、`render_pending` 或被 pin 的 turn 驱逐数为 0。

### 10.2 本地合成基准门槛

| 指标 | 门槛 |
|---|---:|
| 50k 字符 TUI 增量 stream update | p95 < 16 ms，max < 50 ms；mutable tail ≤ 16 KiB |
| 50k 字符 Desktop 增量 stream update | p95 < 16 ms，max < 50 ms；mutable tail ≤ 16 KiB |
| 50k 字符 TUI canonical commit | worker + install p95 < 1 s；事件循环 heartbeat max gap < 50 ms |
| 50k 字符 Desktop canonical commit | worker + install p95 < 1 s；单次主线程 task < 50 ms，DOM mutation slice ≤ 8 ms |
| Desktop 切换 recent window | max < 100 ms，且无持续 > 50 ms long task |
| 正常 turn completed 网络 transcript bytes | 0；仅 patch/new item |
| 10k 节点 recovery snapshot | 分页；单页 ≤ 40 turns，单任务 < 50 ms |
| TUI 30-row viewport Rich 转换 | ≤ 62 visual rows（含 32 overscan） |
| 慢 PTY 1 MB/s、10 MB committed output | heartbeat max gap < 50 ms；输出字节完全一致 |
| 8 MB bracketed paste | 主循环单次处理 max < 50 ms，峰值内存 < 2.5 × payload |
| Desktop mounted DOM | ≤ visible turn window + 10 turns overscan + pinned turns |
| TUI live OutputTree | 稳态 ≤ 20 retained root turns 或 16 MB projected body |

运行：

```bash
./python.py scripts/benchmark_cross_ui_performance.py \
  --stream-chars 50000 \
  --snapshot-nodes 10000 \
  --terminal-rate 1048576

cd frontend
npm exec vitest -- bench test/performance/cross-ui-performance.bench.ts
```

这里直接使用 Vitest `bench` 子命令，因为 `./test.py --frontend` 固定调用 `vitest run`，用于回归测试而非 benchmark。

benchmark 必须输出机器、Python/Node 版本、p50/p95/max、增量/commit 分项、worker/install 时长、long-task/heartbeat、payload bytes、DOM count、mutable tail、Rich conversion count、terminal queue/spool bytes。禁止读取真实 session。

### 10.3 回归命令

Focused tests 通过后：

```bash
./test.py --backend -- src/tests/test_presentation tui/tests -v
./test.py --frontend
```

协议或 Gateway 改动后：

```bash
./test.py --backend -- src/tests/test_contracts src/tests/test_presentation/gateway src/tests/test_presentation/protocol -v
./python.py scripts/export_ui_protocol_schema.py
./test.py --frontend
```

最终：

```bash
./test.py --backend
./test.py --frontend
./test.py --desktop
```

即使默认不修改 Rust，最终仍运行 desktop suite，证明 bundle shell 与新 frontend/protocol 没有集成回归。

## 11. 发布顺序与兼容

### 11.1 提交顺序

1. 合成 benchmark 与复杂度 RED tests；
2. TUI viewport-first + RenderPlan；
3. Desktop rAF + local incremental Markdown；
4. TerminalOutputWriter；
5. capability negotiation；
6. stream append/replace revision；
7. workspace patch；
8. transcript page + keyed reconciliation/window；
9. TUI input/paste/candidate/exit；
10. durable live history eviction。

每项独立提交，不把 local endpoint 优化与 protocol migration 混在同一提交。

### 11.2 Feature gates

内部 feature gates：

- `stream_append_v1`：由 client capability 控制；
- `workspace_patch_v1`：由 client capability 控制；
- `transcript_window_v1`：由 client capability 控制；
- TUI viewport-first/RenderPlan 默认开启，可通过内部 debug flag 回退 legacy renderer；
- TerminalOutputWriter 出错时自动切 plain synchronous fallback，并记录一次错误；
- live history eviction 在 transcript durable watermark 不可用时自动禁用。

### 11.3 回滚

- 新 frontend + 旧 backend：capability RPC method-not-found 时自动 legacy；
- 旧 frontend + 新 backend：server 未收到能力声明，继续 full-replace/full snapshot；
- append revision 异常：client 请求 snapshot，不尝试修补未知 gap；
- window/reconciliation 异常：清空当前 thread DOM 并 render recent canonical snapshot；
- terminal writer 异常：停止接收新 async frame，恢复 synchronous plain output，shutdown barrier 优先；
- eviction 异常：关闭 eviction，不删除 durable transcript。

## 12. 风险与缓解

| 风险 | 后果 | 缓解 |
|---|---|---|
| Markdown stable boundary 判断错误 | 流式预览与最终文本不同 | 不确定 block 留在 mutable tail；commit canonical full render；差分测试 |
| append/full 客户端混用 | 文本重复或缺失 | per-client capability + revision/base_revision；legacy 独立编码 |
| full snapshot 频率降低暴露丢 event | Desktop 状态漂移 | workspace/item revision；gap recovery；定期仅 metadata checksum |
| DOM virtualization 高度估算变化 | prepend/page 时滚动跳动 | ResizeObserver 更新 placeholder；anchor item + pixel offset |
| terminal writer 顺序错误 | frame 覆盖 scrollback 或 terminal 未恢复 | typed batch + sequence + barrier；慢 PTY integration tests |
| writer 队列内存增长 | 大 tool output OOM | committed spool；queue bytes 指标；frame coalescing |
| live tree eviction 过早 | snapshot/page 缺历史 | durable watermark + committed + unreferenced 五条件 |
| candidate 异步结果乱序 | panel 显示旧 query | generation token；只接受当前 generation |
| benchmark 在 CI 波动 | 不稳定测试 | CI 断言操作计数/挂载量；绝对耗时仅本地/专用性能任务 |
| protocol schema 膨胀 | 维护成本增加 | 只增加三个明确 capability；保持 snapshot canonical fallback |

## 13. 明确禁止的捷径

- 不得只把 Desktop debounce 从 100ms 提高来隐藏 O(n²) full render；
- 不得在 stream 中关闭 DOMPurify 或跳过 commit canonical render；
- 不得把 `workspace.snapshot` 继续作为普通状态栏刷新事件；
- 不得在 revision gap 时直接拼接 suffix；
- 不得用 `innerHTML +=` 实现增量 DOM；
- 不得为了 TUI viewport 裁剪而丢失完整 canonical raw text；
- 不得从 terminal writer 丢弃 committed output、restore 或 clear barrier；
- 不得在 transcript 未 durable 时驱逐 live turn；
- 不得在按键事件里同步等待文件、skill 或 MCP provider；
- 不得修改 generated `frontend/src/rpc/protocol.d.ts`，必须通过 schema exporter 生成；
- 不得把真实 `~/.voidx/sessions`、用户 transcript 或本地路径加入 benchmark fixture。

## 14. 完成定义

全部满足后，本增补才可视为完成：

1. 原方案文档未被修改，原方案三个阶段仍可独立实施；
2. P0/P1/P2 focused tests 和 backend/frontend/desktop suites 全绿；
3. TUI 与 Desktop 50k 字符 stream 达到第 10 节门槛；
4. 支持 patch 的 client 正常 turn 完成不构造、不发送完整 transcript snapshot；
5. legacy/new client 并存测试证明最终文本和 thread 状态一致；
6. Desktop 10k synthetic nodes 挂载量受 window 限制，prepend 不跳 scroll；
7. 慢 PTY 下 stdin/cancel heartbeat 达标，commit 字节和 terminal restore 顺序正确；
8. 8MB paste、50k active lines、大目录候选均有复杂度回归测试；
9. durable eviction 后 recent TUI、Desktop page restore 和 persistence canonical state 等价；
10. metrics 能区分 stream mutable tail、snapshot、DOM、terminal queue 和输入候选耗时；
11. 完成实际实现并通过最终 verify 后，才按项目规则移动/归档文档。
