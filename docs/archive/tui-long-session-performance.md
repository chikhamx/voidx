---
name: tui-long-session-performance
display_name: TUI Long-session Performance Optimization
description: 消除 TUI 长会话中全历史渲染、全量 transcript 快照和 UI 事件循环饥饿导致的输入延迟
doc_type: tech-design
audience: human+llm
status: complete
implementation_status: complete
related_docs:
  - docs/design/cross-ui-performance-addendum.md
---

# TUI 长会话性能优化方案

> **Status: Done** — Archived on 2026-08-30.

## 1. 决策摘要

长会话中按回车后输入框延迟清空，不是输入解析或提交队列本身慢，而是 TUI、UI 事件消费和 transcript 持久化共享同一个 asyncio 事件循环，历史增长后存在多条与完整历史长度相关的同步热路径。

本方案分三阶段实施：

1. **P0：消除 TUI 全历史渲染热路径**
   - `OutputTree` 区分尾部追加、尾部子树更新和必须全量失效的结构变更；
   - 根节点尾部追加只渲染新增块，活动子树更新只原地替换缓存尾部；
   - TUI flush、thinking stream 查询只读取未提交后缀或节点行范围。
2. **P1：将 transcript 持久化改为“每轮增量 + 周期压实”**
   - 正常 turn 结束只追加本轮记录，不再追加整棵树；
   - `replace_transcript` 恢复真正的 replace 语义；
   - 每次 append 后按 25 个完成 turn 或 32 MiB tail 阈值检查，并在当前持久化调用返回前完成压实；
   - 保持现有 JSONL 记录格式和 v1 index 可读，兼容既有会话与旧版本回滚。
3. **P2：避免 UI 事件消费者饿死输入任务**
   - 事件总线按时间或数量预算协作式让出；
   - 只有在“stream update 是累计快照”的协议契约建立后，才合并同一 stream 的连续中间帧；
   - 不丢弃工具、权限、checkpoint、turn 生命周期等语义事件。

**核心性能不变量：在终端宽度稳定、没有 reset/restore/reorder 的正常交互路径中，输入回显、新 turn 首帧和 stream 更新耗时必须只与当前活动尾部大小相关，不得与已提交历史长度相关。**

## 2. 状态

- 状态：Complete（运行时基线已实现，剩余跨端增强已转交）
- 收口日期：2026-08-29
- 目标平台：TUI；共享的 `OutputTree`、UI event bus 和 transcript persistence 不改变 Web/Desktop 可见语义
- 后续工作的单一事实来源：`docs/design/cross-ui-performance-addendum.md`

### 2.1 历史实施记录（2026-08-26）

该阶段先闭环两个可独立验证的性能缺口；以下“未完成”描述只代表当时状态：

- [x] **UiEventBus 协作式公平调度**：`_run()` 默认同时受 32 个 ready event 与 4 ms 时间预算约束，达到任一预算后 `await asyncio.sleep(0)`；支持注入 monotonic clock，保持 FIFO、request future、drain/stop、异常传播和事件语义不变。
- [x] **TUI viewport-first 有界渲染**：活动 transcript 与 panel/choice 等底部渲染在 Rich 转换前从尾部按逻辑行取 viewport + 默认 overscan（8..32）候选；保留最终可见尾部、异常 markup 的 `Text(line)` 回退和输入原文状态。
- 当时验证：`tui/tests/test_frame_advanced.py` 20 passed；`test_ui_events_dock_bus.py` 18 passed；相关 presentation + TUI 集合 754 passed。
- 截至当时仍未完成：OutputTree 尾部缓存、transcript 增量持久化与压实、stream contract/coalescing、队列指标和统一 benchmark。

### 2.2 历史实施记录（2026-08-28）

该阶段继续闭环 TUI stream canonical commit 与退出顺序；以下“未完成”描述同样只代表当时状态：

- [x] **异步 canonical commit**：canonical projection 在线程中构建，安装前校验 node、revision、generation，stale 结果丢弃，worker 异常回退 escaped plain projection。
- [x] **安全 scrollback barrier**：pending commit 不进入 native scrollback；cleanup 在停止事件总线前 drain commit tasks。
- [x] **安全退出顺序**：force flush/writer flush 后恢复 terminal，最后在线程中导出兼容 `transcript.log`。
- 当时验证：目标 backend 75 passed；相关 presentation + TUI 集合 779 passed；frontend 677 passed。
- 截至当时仍未完成：OutputTree 尾部缓存、transcript 增量持久化与压实、stream contract/coalescing、慢 PTY/backpressure 和统一 benchmark。

### 2.3 最终收口记录（2026-08-29）

原方案负责的运行时基线现已落地：

- [x] `OutputTree` root-tail append、subtree-tail splice、O(1) 尾部 sibling flag 更新、node range/root slice 与安全 full-render fallback；
- [x] TUI 只转换 viewport/新增 settled 后缀，thinking 查询不扫描完整 line map；
- [x] transcript 按完整 turn 原子追加，支持幂等重试、损坏/incomplete tail 恢复、index 重建、原子 replace 和旧重复 snapshot 压实；
- [x] `UiEventBus` 同时具备数量/时间预算、公平让出、累计快照契约、安全 coalescing、barrier/request future 保序和基础队列指标；
- [x] 异步 canonical commit、scrollback barrier 与 TerminalWriter backpressure 共同保护 TUI 事件循环和终端输出顺序。

本次收口新鲜验证：

```bash
./test.py --backend -- \
  src/tests/test_presentation/output/test_tree_incremental.py \
  src/tests/test_presentation/adapters/persistence/test_transcript_adapter.py \
  src/tests/test_presentation/adapters/persistence/test_transcript_snapshot.py \
  src/tests/test_presentation/gateway/test_ui_events_dock_bus.py \
  src/tests/test_presentation/gateway/test_ui_events_streaming.py \
  tui/tests/test_frame_advanced.py \
  tui/tests/test_frame_rendering.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_run_once.py -v
```

结果：112 passed。

归档门禁验证（2026-08-30）：

- 上述聚焦命令新鲜通过：112 passed；
- `./test.py --backend` 新鲜通过：5236 passed, 30 skipped；
- Gateway P1 聚焦回归新鲜通过：backend 45 passed、frontend 53 passed；
- 归档前引用扫描：tracked docs 中未发现指向设计阶段旧位置的引用，相关文档已指向当前归档文件；
- `git diff --check`：通过；
- 独立审查确认本文的运行时基线、benchmark 转交和压实并发边界内容可归档；两轮 Gateway/跨端历史边界反馈均已按源码修正。

原提案中的独立绝对耗时 benchmark、完整慢路径指标，以及活动 Markdown、Desktop window、输入/paste/candidate 和 durable eviction 等增强项，**不在本归档中伪装为完成**；它们已由 `docs/design/cross-ui-performance-addendum.md` 统一接管。因此本文档不再拥有未闭环实施项，可以按已完成基线归档。

## 3. 实施前问题与证据（2026-08-14）

本节和第 4 节记录设计立项时的基线，不描述归档时的当前实现；当前事实以第 2.3 节和第 9 节为准。

### 3.1 实施前用户可见现象

长 session 多轮对话后，用户在 TUI 输入文本并按回车：

- 输入框不会立即清空；
- 用户消息不会立即出现；
- 延迟随会话历史增长而加重。

这说明延迟发生在原始输入字节被事件循环处理之前，或发生在 `_process_input()` 返回后的同步首帧渲染中，而不是模型请求阶段。

### 3.2 实施前调用链

回车路径：

```text
PureTui.run
  -> _read_input_raw
  -> _process_input
  -> _do_submit
  -> _clear_input
  -> _render_after_input
  -> _render_frame
```

异步刷新路径：

```text
BottomInputDock.refresh
  -> PureTui._on_dock_refresh
  -> PureTui.invalidate
  -> PureTui._run_scheduled_render
  -> PureTui._flush_committed
  -> OutputTree.render
  -> PureTui._render_frame
```

在 2026-08-14 设计基线中，结构变更由 `OutputTree.add_node()` 调用无参数 `mark_dirty()`，使下一次 `render()` 进入 `_full_render()`；新 user turn、首个 assistant stream、工具节点和多数根节点追加都会走此路径。该行为已由第 9.1 节所述增量实现替代。

### 3.3 基准结果

基准环境：2026-08-14、macOS arm64、当时的设计基线源码和匿名化长会话 checkpoint；测试只读取节点结构，不读取或输出对话正文。以下数据是立项证据，不是归档后的性能结果。

真实长会话规模：

| 指标 | 数值 |
|---|---:|
| 完成 turn | 约 76 |
| checkpoint 节点 | 9,363 |
| 100 列下渲染行 | 47,795 |
| checkpoint 大小 | 约 29 MB |
| transcript JSONL | 约 705 MB / 227,200 行 |
| 完整 snapshot 次数 | 51 |

关键耗时：

| 操作 | 耗时 |
|---|---:|
| `_process_input(b"\r")` | 约 0.05 ms |
| 历史已提交时回车后的输入区重绘 | 约 0.33 ms |
| 新 turn 后 `_flush_committed()` | 约 924 ms |
| 首个 assistant stream 后 `_flush_committed()` | 约 988 ms |
| 全部历史误入 active frame 时重绘 | 约 3.2 s |
| `tree_to_transcript_rows()` | 约 61 ms |
| 完整 snapshot 内存建模 | 约 88 ms，事件循环最大停顿约 108 ms |
| 完整 snapshot 临时目录落盘 | 约 375 ms，写入约 58 MB |

对照结果表明：

- 回车解析、输入清空、提交上下文构造本身不是瓶颈；
- 稳态的增量 stream 更新约为数毫秒，但新结构第一次出现会触发接近 1 秒的全树重建；
- transcript 每轮保存完整树会造成二次放大：主线程建模、线程内大 JSON 编码、磁盘 fsync 和文件持续膨胀；
- `UiEventBus._run()` 在队列非空时可连续处理同步 consumer，缺少显式让出，也会让 stdin reader 排队。

## 4. 实施前根因（2026-08-14）

### 4.1 根节点追加被当作任意结构变更

设计基线中的 `src/voidx/presentation/output/tree.py` 只有两类失效状态：

- `_dirty=True`：完整重建；
- `_dirty_nodes`：内容更新时重建一个子树并 splice。

`add_node()` 无法表达“只在已渲染树尾部追加”，因此每个新 turn 或首个 stream 节点都会把整棵树标记为 dirty。历史越长，`_walk_render()`、Rich markup 可见宽度计算、line map/click map 重建越慢。

### 4.2 实施前增量 splice 仍复制或扫描完整缓存

设计基线中的 `OutputTree._incremental_render()` 通过列表拼接重建 `_cached_lines`，并遍历完整 `_node_ranges`、`_line_map`、`_click_map` 修复偏移。即使 dirty 节点位于尾部，仍有 O(历史行数) 的复制或 map 扫描。

### 4.3 TUI 查询 thinking stream 时扫描全量 line map

设计基线中的 `BottomInputDock.active_thinking_stream_line_ids()` 和 `active_thinking_stream_lines()` 遍历完整 `_line_map`/完整 lines；节点范围虽然已存在，但尚未用于范围读取。

### 4.4 实施前 `replace_transcript` 实际执行 append

`src/voidx/presentation/adapters/persistence/transcript_snapshot.py` 中：

```text
replace_transcript
  -> _write_transcript_jsonl_snapshot
  -> append_session_records
```

设计基线在每轮结束时会再次追加 reset、所有 turn 和所有 node，再重写完整 checkpoint；文件大小接近历次快照大小之和，而不是当前 transcript 大小。该行为已由原子 replace 和 turn transaction append 替代。

### 4.5 UI event bus 可连续占用事件循环

设计基线中的 `UiEventBus._run()` 每处理一个同步事件后立即读取下一个队列项；队列积压时循环没有强制让出点。该行为已由数量/时间预算和 `await asyncio.sleep(0)` 替代。

## 5. 历史目标与非目标

### 5.1 目标

1. 10k 节点、50k 渲染行量级下，回车后输入框在一帧内清空。
2. 正常新 turn 和首个 stream 不再遍历已提交历史。
3. transcript 文件增长与新增 turn 大小成正比，不再与“历史大小 × turn 数”成正比。
4. 既有 transcript 可恢复；异常中断不能让已完成 turn 丢失。
5. Web/Desktop 看到的节点层级、顺序、collapse、stream 最终文本保持不变。
6. 建立可重复的合成长会话基准，不使用开发者本机用户会话作为测试夹具（该绝对 benchmark 未在本设计内落地，已按第 9.5 节转交）。

### 5.2 非目标

- 不修改模型消息历史、LLM context compaction 或 session 消息语义；
- 不重写 TUI 为其他 UI 框架；
- 不删除 native terminal scrollback；
- 不在 P0/P1 改 WebSocket protocol；
- 不通过降低历史可恢复性来换取速度；
- 不丢弃工具、权限、checkpoint、clarify、todo、turn start/end 等语义事件；
- 不承诺 resize、restore、任意节点移动和 collapse-all 也完全 O(活动尾部)，这些操作可以受控地回退到全量渲染。

## 6. 必须保持的不变量

### 6.1 输出树

- `OutputTree` 仍是节点层级、line map 和 click map 的单一事实来源；
- 同一宽度下，增量结果必须逐行等于强制 `_full_render()` 的结果；
- `node_ranges` 必须覆盖节点实际渲染范围；
- 根节点尾部追加不得改变已渲染前缀；若该条件不成立，必须回退全量渲染；
- 已提交到 terminal scrollback 的内容不被 TUI 重写；`/clear`、reset 和 restore 除外。

### 6.2 transcript

- 只有收到并处理 `TurnCompleted` 后，turn 才能作为完成事务持久化；
- append 顺序必须是 `turn_start -> node* -> turn_end`；
- index 必须在 JSONL fsync 成功后更新；
- index 缺失或大小不匹配时，loader 必须能扫描 JSONL 恢复；
- 扫描异常尾部时，只提交拥有 `turn_end` 的完整 turn；
- 现有 `transcript_reset`、`summary`、`node` 记录保持可读。

### 6.3 UI 事件

- 单个 stream 的 committed 文本必须与未优化版本完全一致；
- 不得跨越 commit/discard、tool、permission 或 turn lifecycle barrier 合并事件；
- `UiEventBus.request()` 的 future 必须由对应事件处理结果完成，不能被 coalescing 替代。

## 7. 历史设计方案与最终实现差异

本节保留获批时的设计意图；若其中未来式表述与当前源码不同，以第 9 节实现映射和权威源码为准。

### 7.1 P0：输出树尾部增量渲染

#### 7.1.1 失效类型

在 `OutputTree` 内部区分以下变更：

| 类型 | 示例 | 渲染策略 |
|---|---|---|
| `tail_append` | root 末尾追加 spacer/turn；活动 agent 末尾追加 stream/tool | 追加新块或重绘最小尾部父子树 |
| `content_update` | 已有 stream 文本更新、status 文本更新 | 重绘目标子树；若位于缓存尾部则原地 tail splice |
| `general_structure` | 插入到中间、移动、删除、collapse/expand、restore、宽度变化 | 全量渲染回退 |

不要求把该枚举暴露为公共 API；可以使用内部状态和专用方法实现，但调用点必须能表达上述语义。

#### 7.1.2 根节点尾部追加快路径

`OutputTree` 记录最近一次成功 render 时已处理的 root child 边界。满足以下条件时只渲染新增 root children：

- width 与 `_cached_width` 相同；
- 现有 root children 未被重排或删除；
- 新节点只追加在 root 尾部；
- 旧缓存处于 clean 状态；
- gap 规则只依赖“旧最后可见 child + 新 child”。

追加时需要：

1. 按 `_needs_gap_between_root_blocks()` 计算旧尾部与第一个新块之间的 gap；
2. 只对新增 child 调用 `_walk_render()`；
3. 将新 lines、line map、click map 和 node ranges 按旧行数偏移后追加；
4. 更新 rendered root child 边界；
5. 保持旧前缀对象和值不变。

若检查失败，调用现有 `_full_render()`，确保正确性优先。

#### 7.1.3 活动子树尾部 splice

活动 agent 下新增 tool/stream 时，前一个 sibling 的 connector 可能从 last 变为 non-last。此时不能只画新 child，应将父节点标记为结构 dirty，并重绘该父子树。

如果父子树旧范围到达 `_cached_lines` 尾部：

- 使用 list slice assignment 原地替换尾部，不通过 `prefix + new + suffix` 复制完整历史；
- 只删除和重建旧范围内的 line map/click map；
- 只修复该父子树内部 node ranges；
- 不遍历 old range 之前的 map/range。

只有 dirty range 后方还有已渲染节点时，才使用通用 splice 或全量回退。

#### 7.1.4 sibling flag 更新

`OutputNode.add_child()` 不再循环清除全部旧 children 的 `_is_last_sibling`。尾部追加只需：

1. 将原最后一个 child 设为 `False`；
2. 将新 child 设为 `True`。

中间插入或重排继续调用 `_refresh_sibling_flags()`。

#### 7.1.5 范围读取 API

为 `OutputTree` 增加只读范围能力，名称可在实现时微调：

```python
def node_line_range(self, node_id: str, console_width: int) -> tuple[int, int] | None: ...
def render_slice(self, console_width: int, start: int, end: int | None = None) -> list[str]: ...
```

要求：

- 先确保当前 cache 已按最小失效范围更新；
- `active_thinking_stream_line_ids()` 由 node range 生成范围，不扫描完整 line map；
- `active_thinking_stream_lines()` 直接 slice 节点范围；
- TUI 的 active transcript 只 slice `_committed_line_count:`。

#### 7.1.6 TUI flush 路径

`PureTui._flush_committed()` 保持 scrollback 语义，但不得主动触发已提交前缀的 Rich 转换。流程调整为：

1. 请求 OutputTree 更新 cache；正常路径只更新尾部；
2. 从 `_committed_line_count` 开始计算 safe flush limit；
3. 只将本次新增 settled lines 转成 Rich `Text`；
4. 更新 `_committed_line_count`；
5. `_render_frame()` 只渲染剩余 active tail 和 bottom dock。

全量回退只允许出现在：首次 render、width 变化、reset/restore、任意中间插入/删除/移动、显式 collapse/expand。

#### 7.1.7 P0 正确性保护

测试模式增加“增量结果对照全量结果”：对同一 mutation 序列分别运行增量 render 和强制 full render，断言：

- lines 完全相等；
- line map 完全相等；
- click map 完全相等；
- 每个可见节点 range 相等。

生产环境若检测到边界、range 或 root child 版本不一致，记录一次慢路径原因并回退 full render，不得输出损坏帧。

### 7.2 P1：transcript 每轮增量持久化

#### 7.2.1 保持现有文件和记录格式

继续使用：

- `transcript.jsonl`
- `transcript.idx.json`
- `transcript.checkpoint.json`

继续使用现有 JSONL record：

- `transcript_reset`
- `turn_start`
- `node`
- `turn_end`
- `summary`

这样旧版本在回滚后仍可以通过扫描 JSONL 恢复，不引入必须同步升级的数据库或 protocol migration。

#### 7.2.2 正常 turn 只追加 delta：最终实现差异

获批方案要求按 turn 导出，并以物理 JSONL 中最后一个拥有 `turn_end` 的完整事务作为 durable 事实；index 只作为可重建的恢复加速器。最终实现保留了这些不变量，但没有新增方案中设想的独立单锁 helper：

- `TranscriptSnapshotAdapter.persist_current()` 发现尚未 durable 的完成 turn，并通过 `tree_to_transcript_turn_rows()` 只导出对应 root block；
- `append_transcript_turns()` 持有可重入的 `session_directory_locks()`，在同一锁域内校验文件大小与 index；
- index 缺失或失配时扫描物理 JSONL，重建完整事务边界；
- 已完整存在的 turn 被过滤，缺失 turn 以单批 `turn_start/node*/turn_end` 追加并 fsync；
- append 成功后仍在同一锁域内更新 `transcript_size`、offset 和 index；嵌套调用的 `append_session_records()` / `write_session_json()` 复用已持有的 session lock。

因此，即使进程在 JSONL fsync 后、index 更新前崩溃，重试也会先从物理文件识别已完成事务并修复 index，不重复追加同一 turn；一次恢复发现多个缺失完成 turn 时可在一个 append batch 中补齐。

#### 7.2.3 `replace_transcript` 恢复真实 replace 语义

`replace_transcript()` 只保留给显式全量替换、测试或压实流程。其实现必须：

1. 将单份完整 snapshot 写到临时 JSONL；
2. flush + fsync；
3. `os.replace()` 原子替换正式 JSONL；
4. 写 checkpoint；
5. 最后原子更新 index。

不得再调用 append 写入完整 snapshot。

#### 7.2.4 checkpoint 与压实：最终实现差异

增量 JSONL 是 durable source，checkpoint 是恢复加速器。最终实现没有创建 session-runtime 后台任务：`append_transcript_turns()` 完成事务和 index 更新后直接 `await maybe_compact_transcript()`；达到 25 个完成 turn 或 32 MiB tail 任一阈值时，在当前持久化调用返回前加载 canonical rows，并通过原子 `replace_transcript()` 完成压实。

这只保证单次正常持久化调用中先完成 append/index 提交，再顺序等待压实；已 fsync 的 turn 不因压实失败而回滚。`compact_transcript()` 的 canonical load 与原子 replace 分别获取 session lock，当前实现没有把两步合成一个跨调用事务，因此本文不宣称并发 append 与压实已被完整串行化；该增强与后台生命周期管理一并转交 `docs/design/cross-ui-performance-addendum.md`。

#### 7.2.5 崩溃恢复

loader 调整：

- index 与文件大小匹配：checkpoint + checkpoint offset 后的 tail；
- index 不匹配：从最后可信 reset/checkpoint 扫描并重建 index；
- tail 中以 `turn_start` 开始但没有 `turn_end` 的 turn 不进入最终 rows；
- 重复 append 的同一 `(turn_id, node_id)` 使用最后一个完整 turn 事务；
- JSON 尾行损坏时忽略该尾行，并记录一次内部错误。

#### 7.2.6 旧会话兼容与空间回收

不做阻塞式批量迁移。

首次打开旧会话时：

1. 优先使用有效 checkpoint/index 或扫描 JSONL 恢复；
2. 新 turn 按完整 transaction 增量 append；
3. append 后达到 25-turn/32-MiB 阈值时，在当前调用内执行 canonical replace 压实；
4. 压实成功后，旧版重复 snapshot 被收敛为单份 reset/canonical transcript，恢复 rows 保持一致。

任何测试不得复制开发者 `~/.voidx/sessions` 数据；使用合成节点和临时 `VOIDX_HOME`。

### 7.3 P2：UI 事件公平调度与安全合并

#### 7.3.1 协作式让出

`UiEventBus._run()` 增加 batch budget：

- 连续处理最多 32 个 ready event；或
- 连续同步处理达到 4 ms；
- 任一条件满足后执行 `await asyncio.sleep(0)`。

具体数值可依据 benchmark 微调，但必须同时有“数量上限”和“时间上限”。`request()` future、FIFO 和错误传播语义保持不变。

#### 7.3.2 stream update 合并的前置契约

设计基线中的 `AssistantStreamUpdated.text` schema 只声明 `str`，尚未明确累计全文契约；因此设计要求先建立并测试以下协议，再启用合并：

> 对同一 `(thread_id, agent_id, stream_id, phase)`，每个 `AssistantStreamUpdated.text` 都是截至该事件的完整累计快照。

只有所有生产者和 contract test 满足该语义后，才允许合并连续 update。

#### 7.3.3 合并规则

可合并键：

```text
(thread_id, agent_id, stream_id, phase)
```

仅合并队列中连续、同键的 `AssistantStreamUpdated`，保留最后一个。以下事件是 barrier，不得跨越：

- `AssistantStreamCommitted`
- `AssistantStreamDiscarded`
- `TurnStarted` / `TurnCompleted` / `TurnCancelled` / `TurnFailed`
- tool、permission、checkpoint、clarify、todo 生命周期事件
- 带 request future 的事件

第一版不对 `StatusUpdated` 做合并，除非另行证明其为纯快照语义。

#### 7.3.4 队列策略

第一版不设置会丢语义事件的硬容量。增加软阈值：

- queue depth 超过 256：记录 rate-limited warning；
- queue depth 超过 1,000：仍不丢 barrier event，但优先执行合法 stream coalescing；
- 记录 oldest event age、batch duration 和 yield 次数。

### 7.4 可观测性

现有 `RenderStats` 扩展或配套记录以下字段：

| 指标 | 含义 |
|---|---|
| `render_strategy` | full / root-tail-append / subtree-tail-splice / generic-splice |
| `history_lines` | 完整缓存行数 |
| `active_lines` | 未提交活动行数 |
| `render_ms` | 本次 render 总耗时 |
| `full_render_reason` | width-change / restore / reorder / invariant-fallback 等 |
| `ui_queue_depth` | UI event queue 当前深度 |
| `ui_oldest_event_ms` | 最旧事件等待时间 |
| `transcript_delta_rows` | 本轮追加 rows |
| `transcript_delta_bytes` | 本轮追加字节 |
| `transcript_checkpoint_ms` | checkpoint/压实耗时 |

只对超过阈值的慢操作写 rate-limited internal log，避免日志本身成为新瓶颈。建议阈值：render 50 ms、event age 100 ms、snapshot event-loop gap 50 ms。

## 8. 历史文件改动范围与转交

### 8.1 P0

| 文件 | 责任 |
|---|---|
| `src/voidx/presentation/output/tree.py` | 失效分类、root tail append、tail splice、范围读取、sibling flag 优化 |
| `src/voidx/presentation/output/dock/app.py` | safe flush 和 thinking stream 使用节点范围/尾部 slice |
| `src/voidx/presentation/output/dock/stream.py` | 新 stream 与内容更新使用精确 dirty 语义 |
| `src/voidx/presentation/output/dock/nodes.py` | 工具/status 节点变更使用精确 dirty 语义 |
| `tui/voidx_cli/app.py` | `_flush_committed()` 只转换新增 settled 后缀，记录慢路径原因 |
| `tui/voidx_cli/render_frame.py` | active tail 渲染与 render strategy 统计 |
| `tui/voidx_cli/state.py` | 如需要，扩展 render stats 字段 |
| `src/tests/test_presentation/output/test_tree_incremental.py`（新建） | 增量与 full 等价、范围/map 不变量、复杂度回归 |
| `tui/tests/test_frame_advanced.py` | 长历史下提交与首 stream 不重绘旧前缀 |
| `tui/tests/test_frame_rendering.py` | scrollback、resize、full fallback 回归 |

### 8.2 P1

| 文件 | 责任 |
|---|---|
| `src/voidx/presentation/adapters/persistence/transcript_snapshot.py` | turn delta 导出、append、完整事务恢复、checkpoint/压实 |
| `src/voidx/presentation/adapters/persistence/transcript_adapter.py` | durable turn 发现与按 turn 导出 |
| `src/voidx/persistence/jsonl.py` | session lock、原子 replace 与 fsync 语义 |
| `src/tests/test_presentation/adapters/persistence/test_transcript_adapter.py` | 增量增长、旧格式恢复、压实、崩溃尾部测试 |
| `src/tests/test_persistence/test_jsonl_store.py` | 原子 replace 和失败安全测试 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_session_run_once.py` | turn 完成后 transcript 保存/恢复端到端回归 |

### 8.3 P2

| 文件 | 责任 |
|---|---|
| `src/voidx/presentation/output/events/bus.py` | batch budget、协作式 yield、合法 stream coalescing、队列指标 |
| `src/voidx/agent/domain/ui_events.py` | 明确 `AssistantStreamUpdated.text` 累计快照契约；若需要增加兼容字段 |
| stream event 生产者 | 保证累计快照语义 |
| `src/tests/test_presentation/gateway/test_ui_events_dock_bus.py` | 公平调度、barrier、request future、队列错误语义 |
| `src/tests/test_presentation/gateway/test_ui_events_streaming.py` | 累计快照 contract 与最终文本等价 |

### 8.4 统一基准与观测（已转交）

原计划的 `scripts/benchmark_tui_long_session.py` 未单独落地，不能作为本归档的通过证据。长会话绝对耗时、跨端统一夹具和完整慢路径指标已合并到 `docs/design/cross-ui-performance-addendum.md`，由该文档继续闭环；本设计以可重复的复杂度断言和聚焦回归验证运行时基线。

## 9. 实施结果

本节是最终实现映射，不再作为待执行任务清单。

### 9.1 P0：输出树与 TUI 渲染基线

- [x] `OutputTree` 支持 root 尾部追加、dirty subtree 尾部 splice 和 O(1) 尾部 sibling flag 更新；不满足快路径不变量时回退 full render。
- [x] node range 与 root slice 可直接读取局部结果；thinking stream 查询不扫描完整 line map。
- [x] 增量 subtree 更新与 full render 的 lines、line map、click map、node ranges 等价，collapse 会清理 stale descendant ranges/maps。
- [x] TUI 在 Rich 转换前执行 viewport-first 裁剪，10,000 行历史的转换量受 viewport + overscan 约束；restore/resize 等路径保持安全回退。
- 主要实现：`src/voidx/presentation/output/tree.py`、`src/voidx/presentation/output/dock/app.py`、`tui/voidx_cli/render_frame.py`。
- 主要回归：`src/tests/test_presentation/output/test_tree_incremental.py`、`tui/tests/test_frame_advanced.py`、`tui/tests/test_frame_rendering.py`。

### 9.2 P1：transcript 增量持久化基线

- [x] 正常持久化只追加尚未 durable 的完整 turn transaction，不重复追加全部历史。
- [x] append 以 `turn_start -> node* -> turn_end` 为事务边界，重复 turn 幂等；失败后 cursor 不前移并能补齐缺失 turn。
- [x] loader 丢弃 incomplete/损坏 tail 并重建 index；显式 replace 使用原子覆盖语义。
- [x] legacy 重复 snapshot 可压实为单份 reset，压实前后恢复 rows 等价且文件缩小。
- [x] session runtime 的持久化与恢复端到端回归已覆盖。
- 主要实现：`src/voidx/presentation/adapters/persistence/transcript_snapshot.py`、`transcript_adapter.py`、`src/voidx/persistence/jsonl.py`。
- 主要回归：`test_transcript_adapter.py`、`test_transcript_snapshot.py`、`test_session_run_once.py`。

### 9.3 P2：事件公平调度与安全合并基线

- [x] `UiEventBus` 同时受 32 个 ready event 和 4 ms 时间预算约束，达到预算后协作式让出。
- [x] `AssistantStreamUpdated` 明确累计快照契约；仅连续同键 update 可合并并保留最新文本。
- [x] thread/phase、commit/discard、生命周期事件和 request future 构成 barrier，不被跨越或替代。
- [x] 暴露 queue depth、processed、coalesced 基础指标，并覆盖公平调度、合并和 barrier 回归。
- 主要实现：`src/voidx/presentation/output/events/bus.py`、`src/voidx/agent/domain/ui_events.py`。
- 主要回归：`test_ui_events_dock_bus.py`、`test_ui_events_streaming.py`。

### 9.4 配套 TUI 生命周期基线

- [x] canonical stream commit 在线程中构建，安装前校验 node/revision/generation，失败时安全回退。
- [x] `render_pending` 形成 scrollback barrier；cleanup 在停止事件总线前 drain commit tasks。
- [x] TerminalWriter worker 承担慢 stdout，frame 可合并而 commit/barrier 保序，终端恢复先于 transcript 导出。

### 9.5 正式转交项

以下增强不计入本设计的完成状态，由 `docs/design/cross-ui-performance-addendum.md` 继续负责：

- 统一跨端合成 benchmark 与绝对 p50/p95/max 门槛；
- render/event/transcript 的完整慢路径原因、等待时长和字节指标；
- bounded active Markdown、Desktop rAF/Worker/keyed reconciliation/DOM window；
- transcript window、输入/paste/candidate 优化和 durable live-history eviction。

## 10. 性能验收标准

### 10.1 功能测试中的复杂度断言

CI 单测优先断言“旧节点访问次数/旧 line 转换次数为 0”，而不是依赖易波动的绝对毫秒：

- root tail append：旧 `_walk_render()` 节点访问数为 0；
- active subtree tail update：访问范围不超过当前 active subtree；
- thinking stream 查询：不遍历 range 外 line map；
- transcript persist：导出 rows 数等于新增 turn rows，不等于完整树 rows；
- UI event bus：队列未 drain 前 heartbeat 至少运行一次。

### 10.2 绝对耗时目标（历史提案，已转交）

下表保留原设计提出的本地目标，便于后续跨端基准对照；它不是本归档的通过证据，本文档也不声称这些绝对门槛已经测得或通过。

| 指标 | 原目标 |
|---|---:|
| `_process_input(b"\r")` | p95 < 1 ms |
| 回车后输入区重绘 | p95 < 16 ms，max < 50 ms |
| 新 user turn flush + frame | p95 < 25 ms，max < 50 ms |
| 首个 assistant stream flush + frame | p95 < 25 ms，max < 50 ms |
| 后续 stream update | p95 < 16 ms |
| UI event heartbeat max gap | < 50 ms；目标 < 16 ms |
| 正常 turn transcript 主线程建模 | < 8 ms 或仅与本轮节点数线性相关 |
| transcript 文件增长 | `O(新增 turn bytes)` |
| 旧重复 transcript 压实后大小 | 不超过当前单份 snapshot + checkpoint + 10% tail |

原计划的 `scripts/benchmark_tui_long_session.py` 没有创建。统一夹具、可执行命令和绝对耗时验收现由 `docs/design/cross-ui-performance-addendum.md` 第 10 节负责；后续不得引用本表宣称 benchmark 已通过。

### 10.3 回归测试

每阶段 focused tests 通过后运行：

```bash
./test.py --backend -- src/tests/test_presentation tui/tests -v
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_session_run_once.py -v
```

最终运行完整 backend：

```bash
./test.py --backend
```

## 11. 发布与回滚

### 11.1 实施与转交记录

运行时基线最终分三个可追溯提交落地：

1. `a689cc0f`：transcript 增量事务、恢复与压实；
2. `97a19bf1`：OutputTree 尾部增量、UiEventBus 公平调度与安全合并；
3. `c345d0ba`：TUI viewport/canonical 输出与 TerminalWriter backpressure。

原计划的独立 benchmark 没有与这些实现提交绑定，也不作为本归档的完成证据；统一性能基准和剩余增强已转交 `docs/design/cross-ui-performance-addendum.md`。上述提交仍可按功能边界独立回滚。

### 11.2 回滚策略

- P0：保留 `_full_render()`；发现增量不变量失败时立即 fallback，可独立回滚精确 dirty 调用点；
- P1：继续写现有 JSONL record，旧 binary 可扫描恢复；压实必须生成单份完整、旧 reader 可识别的 snapshot；
- P2：关闭 coalescing 后仍保留 batch yield；关闭 batch yield 不影响数据格式；
- 任何持久化升级失败都不得删除旧 JSONL/checkpoint，只有新文件 fsync 且 index 原子提交后才清理临时文件。

## 12. 风险与缓解

| 风险 | 后果 | 缓解 |
|---|---|---|
| root gap/connector 规则遗漏 | 增量输出与 full 不一致 | mutation 序列差分测试；生产 invariant fallback |
| 节点在 settled 后仍被修改 | 已提交 scrollback 与 tree 不一致 | 明确 settled immutable；修改旧节点强制 full/记录错误 |
| width 改变导致 line range 失效 | cursor 或 click map 错位 | width 是 cache key；resize 强制 full |
| append 与 index 更新并发 | index/offset 不一致 | 同一可重入 session lock；JSONL fsync 后提交 index |
| crash 留下半个 turn | 恢复出不完整 UI | `turn_end` 事务边界；异常 tail 丢弃 |
| canonical load 与 replace 之间出现并发 append | 压实 snapshot 可能遗漏新事务 | 当前归档不宣称跨调用串行化；完整压实事务与后台生命周期转交跨端增补 |
| 阈值压实在 append 调用内等待 | 大 transcript 可能拉长持久化尾延迟 | 保持阈值与事务测试；后台化作为跨端增补待办 |
| 错误合并 delta stream | 最终 assistant 文本缺失 | 先建立累计快照 contract；不满足则只做公平让出 |
| 性能测试依赖本机 | CI 波动或泄露用户数据 | 合成夹具；CI 断言复杂度，本地 benchmark 看绝对耗时 |

## 13. 明确禁止的实现捷径

- 不得通过增大 `RENDER_THROTTLE_SECONDS` 掩盖单次 1 秒 full render；
- 不得在主事件循环中对完整 tree 做 Pydantic dump/JSON dump；
- 不得每轮重写完整 transcript 并称为“异步后就没有问题”；
- 不得使用 `queue.maxsize` 后静默丢弃任意 UI event；
- 不得在没有累计快照 contract 时丢弃 `AssistantStreamUpdated`；
- 不得绕过 `voidx.presentation.output.dock` 直接向 stdout 写 UI 内容；
- 不得把开发者现有 `~/.voidx/sessions` 复制进仓库或测试产物。

## 14. 归档判定

本文档按“已提交历史的 TUI 运行时基线”收口，归档条件如下：

1. P0 OutputTree/TUI、P1 transcript、P2 event bus 的实现文件和聚焦回归存在；
2. 增量 render、transcript transaction/recovery/compaction、event fairness/coalescing/barrier 均有行为或复杂度断言；
3. 本轮聚焦命令新鲜通过 112 项测试；
4. 独立绝对 benchmark 与完整慢路径观测明确标为未验收，并转交仍处于 `in-progress/partial` 的跨端增补方案；
5. 归档前运行完整 backend 回归、引用扫描和 `git diff --check`，并把结果写入归档状态说明。

这些条件满足后，本文档可归档为已实现基线；归档不代表第二份跨端增补方案完成。
