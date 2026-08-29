---
name: tui-long-session-performance
display_name: TUI Long-session Performance Optimization
description: 消除 TUI 长会话中全历史渲染、全量 transcript 快照和 UI 事件循环饥饿导致的输入延迟
doc_type: tech-design
audience: human+llm
status: in-progress
implementation_status: partial
---

# TUI 长会话性能优化方案

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
   - checkpoint 周期生成，历史重复率过高时后台压实旧 JSONL；
   - 保持现有 JSONL 记录格式和 v1 index 可读，兼容既有会话与旧版本回滚。
3. **P2：避免 UI 事件消费者饿死输入任务**
   - 事件总线按时间或数量预算协作式让出；
   - 只有在“stream update 是累计快照”的协议契约建立后，才合并同一 stream 的连续中间帧；
   - 不丢弃工具、权限、checkpoint、turn 生命周期等语义事件。

**核心性能不变量：在终端宽度稳定、没有 reset/restore/reorder 的正常交互路径中，输入回显、新 turn 首帧和 stream 更新耗时必须只与当前活动尾部大小相关，不得与已提交历史长度相关。**

## 2. 状态

- 状态：In progress（本轮部分实施，整体未闭环）
- 目标读者：维护者与后续实现 agent
- 目标平台：TUI；共享的 `OutputTree`、UI event bus 和 transcript persistence 同时影响 Web/Desktop，但不得改变其可见语义
- 实施方式：按 P0、P1、P2 独立提交；每阶段必须单独通过测试和性能验收

### 2.1 本轮实施记录（2026-08-26）

本轮只闭环两个可独立验证的性能缺口，本文档**不因此标记完成或归档**：

- [x] **UiEventBus 协作式公平调度**：`_run()` 默认同时受 32 个 ready event 与 4 ms 时间预算约束，达到任一预算后 `await asyncio.sleep(0)`；支持注入 monotonic clock，保持 FIFO、request future、drain/stop、异常传播和事件语义不变。
- [x] **TUI viewport-first 有界渲染**：活动 transcript 与 panel/choice 等底部渲染在 Rich 转换前从尾部按逻辑行取 viewport + 默认 overscan（8..32）候选；保留最终可见尾部、异常 markup 的 `Text(line)` 回退和输入原文状态。
- 验证：`./test.py --backend -- tui/tests/test_frame_advanced.py -v`（20 passed）；`./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_dock_bus.py -v`（18 passed）；相关集合 `./test.py --backend -- src/tests/test_presentation/gateway src/tests/test_presentation/output tui/tests`（754 passed，含 `tui/tests/test_output_tree.py` 17 passed）。

仍未完成的高价值项包括：OutputTree root-tail/subtree-tail 全量缓存优化、thinking/flush 全链路范围化、transcript 每轮增量持久化与压实、stream snapshot contract/coalescing、队列指标/benchmark，以及其他跨端和终端 I/O 设计。

### 2.2 本轮实施记录（2026-08-28）

本轮闭环 TUI stream canonical commit 的异步调度与安全退出顺序，但不改变本文档整体 `in-progress/partial` 状态：

- [x] **异步 canonical commit**：`BottomInputDock.prepare_stream_commit()` 先将 stream 标记为 `render_pending`，`DockEventConsumer` 立即调度后台任务；canonical projection 在线程中构建，安装时通过 node、revision、generation 校验丢弃 stale 结果，worker 失败时回退 escaped plain projection。
- [x] **安全 scrollback barrier**：pending commit 不进入不可修改的 native scrollback；`TerminalRunLoop.cleanup_run_loop()` 在停止事件总线前 drain commit tasks，reset/discard 后的旧结果不会回写。
- [x] **安全退出顺序**：`PureTui.run()` finally 先 force flush/writer flush，再恢复 terminal，写退出序列并 flush，最后通过 `asyncio.to_thread()` 导出兼容 `transcript.log`。
- 验证：目标测试 `./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_streaming.py src/tests/test_presentation/gateway/test_ui_events_dock_bus.py tui/tests/test_input_advanced.py tui/tests/test_terminal_writer.py`（75 passed）；相关 presentation + TUI 集合（779 passed）；`./test.py --frontend`（677 passed）。
- 完整 backend `./test.py --backend`：5163 passed、2 failed、30 skipped；失败为 `test_run_turn_persists_and_restores_transcript_snapshot` 与 `test_run_turn_commits_event_todo_at_turn_end`。当前没有独立基线可证明这两项是既有问题；它们不属于本轮目标路径，未在本轮解决，因此整体不能标记完成。

仍未完成：OutputTree root/subtree 增量缓存、transcript 每轮增量持久化与压实、stream snapshot contract/coalescing、Desktop canonical worker 与协议窗口化、慢 PTY/backpressure、RenderPlan、输入/paste/candidate 优化、队列指标和 benchmark。

## 3. 问题与已验证证据

### 3.1 用户可见现象

长 session 多轮对话后，用户在 TUI 输入文本并按回车：

- 输入框不会立即清空；
- 用户消息不会立即出现；
- 延迟随会话历史增长而加重。

这说明延迟发生在原始输入字节被事件循环处理之前，或发生在 `_process_input()` 返回后的同步首帧渲染中，而不是模型请求阶段。

### 3.2 当前调用链

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

当前结构变更由 `OutputTree.add_node()` 调用无参数 `mark_dirty()`，使下一次 `render()` 进入 `_full_render()`。新 user turn、首个 assistant stream、工具节点和多数根节点追加都会走此路径。

### 3.3 基准结果

基准环境：macOS arm64，使用当前工作区源码和匿名化的真实长会话 checkpoint；测试只读取节点结构，不读取或输出对话正文。

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

## 4. 根因

### 4.1 根节点追加被当作任意结构变更

`src/voidx/presentation/output/tree.py` 当前只有两类失效状态：

- `_dirty=True`：完整重建；
- `_dirty_nodes`：内容更新时重建一个子树并 splice。

`add_node()` 无法表达“只在已渲染树尾部追加”，因此每个新 turn 或首个 stream 节点都会把整棵树标记为 dirty。历史越长，`_walk_render()`、Rich markup 可见宽度计算、line map/click map 重建越慢。

### 4.2 现有增量 splice 仍复制或扫描完整缓存

`OutputTree._incremental_render()` 当前通过列表拼接重建 `_cached_lines`，并遍历完整 `_node_ranges`、`_line_map`、`_click_map` 修复偏移。即使 dirty 节点位于尾部，仍有 O(历史行数) 的复制或 map 扫描。

### 4.3 TUI 查询 thinking stream 时扫描全量 line map

`BottomInputDock.active_thinking_stream_line_ids()` 和 `active_thinking_stream_lines()` 遍历完整 `_line_map`/完整 lines。节点范围已经保存在 `_node_ranges`，但没有被用于范围读取。

### 4.4 `replace_transcript` 实际执行 append

`src/voidx/presentation/adapters/persistence/transcript_snapshot.py` 中：

```text
replace_transcript
  -> _write_transcript_jsonl_snapshot
  -> append_session_records
```

每轮结束会再次追加 reset、所有 turn 和所有 node，然后重写完整 checkpoint。最终文件大小接近历次快照大小之和，而不是当前 transcript 大小。

### 4.5 UI event bus 可连续占用事件循环

`UiEventBus._run()` 每处理一个同步事件后立即读取下一个队列项。当队列已有积压时，`await queue.get()` 可以立即完成，循环没有强制让出点。流式输出密集时，stdin reader 和 TUI render callback 可能长期得不到调度。

## 5. 目标与非目标

### 5.1 目标

1. 10k 节点、50k 渲染行量级下，回车后输入框在一帧内清空。
2. 正常新 turn 和首个 stream 不再遍历已提交历史。
3. transcript 文件增长与新增 turn 大小成正比，不再与“历史大小 × turn 数”成正比。
4. 既有 transcript 可恢复；异常中断不能让已完成 turn 丢失。
5. Web/Desktop 看到的节点层级、顺序、collapse、stream 最终文本保持不变。
6. 建立可重复的合成长会话基准，不使用开发者本机用户会话作为测试夹具。

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

## 7. 设计方案

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

#### 7.2.2 正常 turn 只追加 delta

新增按 turn 导出的函数，名称可在实现时微调：

```python
def tree_turn_count(tree: OutputTree) -> int: ...
def tree_to_transcript_turn_rows(
    session_id: str,
    tree: OutputTree,
    turn_id: int,
) -> list[TranscriptNodeRow]: ...

async def append_transcript_turns(
    session_id: str,
    turns: list[tuple[int, list[TranscriptNodeRow]]],
) -> None: ...
```

`TranscriptSnapshotAdapter.persist_current()` 先从 live tree 取得“已完成 turn id”候选集，但不得仅把进程内计数或 index 当作 durable cursor。**物理 JSONL 中最后一个拥有 `turn_end` 的完整事务才是已持久化事实；index 只是可重建的恢复加速器。**正常情况下只递归导出当前完成 turn 的 root block，不遍历旧节点。

幂等持久化顺序：

1. 在事件循环中只导出候选完成 turn 的 rows，生成 `turn_start/node*/turn_end`；
2. 获取同一 session lock；现有 `append_session_records()` 会自行加锁，不能嵌套调用，需在 `src/voidx/persistence/jsonl.py` 提供一个覆盖“尾部校验 + append + index commit”的单锁 helper；
3. 在锁内核对 index 与真实文件大小；若不一致，从最后可信 checkpoint/reset offset 扫描物理尾部，找出最后一份完整 turn 事务；
4. 过滤已经完整存在的 turn；若尾部只有同 turn 的不完整事务，则保留该异常尾部并追加一份新的完整事务；
5. 对缺失 turn 执行单批 append + flush + fsync；
6. 在仍持有 session lock 时原子更新 index 的 `transcript_size`、`turn_offsets`，并保留原 checkpoint offset；
7. 释放锁；loader 继续使用 checkpoint + checkpoint offset 后的 tail 恢复。

这样即使进程在 JSONL fsync 后、index 更新前崩溃，重试也会先从物理尾部识别已完成事务，只修复 index，不重复追加同一 turn。如果一次恢复后有多个未持久化完成 turn，可在一个 append batch 中补齐。

#### 7.2.3 `replace_transcript` 恢复真实 replace 语义

`replace_transcript()` 只保留给显式全量替换、测试或压实流程。其实现必须：

1. 将单份完整 snapshot 写到临时 JSONL；
2. flush + fsync；
3. `os.replace()` 原子替换正式 JSONL；
4. 写 checkpoint；
5. 最后原子更新 index。

不得再调用 append 写入完整 snapshot。

#### 7.2.4 checkpoint 与压实

增量 JSONL 是 durable source；checkpoint 是恢复加速器。

建议默认触发条件：

- 距上次 checkpoint 新增 25 个完成 turn；或
- checkpoint tail 超过 32 MB；或
- `transcript.jsonl / transcript.checkpoint.json` 大小比超过 4，表明存在旧版重复快照；或
- 用户显式执行维护/压实命令（若后续提供）。

checkpoint/压实要求：

- 不从 live `OutputTree` 在事件循环中构建完整 rows；
- 在线程中读取现有 checkpoint + JSONL tail，重放为当前 rows；
- 写新 checkpoint；
- 需要回收空间时，调用真实 `replace_transcript()` 将 JSONL 压成单份完整 snapshot；
- 正常 turn append 优先于后台压实，二者通过 session lock 串行；
- 后台任务由 session runtime 跟踪，session switch/clean shutdown 时等待或安全取消；
- 压实失败只影响空间回收，不能回滚已 fsync 的增量 turn。

#### 7.2.5 崩溃恢复

loader 调整：

- index 与文件大小匹配：checkpoint + checkpoint offset 后的 tail；
- index 不匹配：从最后可信 reset/checkpoint 扫描并重建 index；
- tail 中以 `turn_start` 开始但没有 `turn_end` 的 turn 不进入最终 rows；
- 重复 append 的同一 `(turn_id, node_id)` 使用最后一个完整 turn 事务；
- JSON 尾行损坏时忽略该尾行，并记录一次内部错误。

#### 7.2.6 旧会话兼容与空间回收

不做阻塞式批量迁移。

首次打开旧会话：

1. 优先使用现有有效 checkpoint 恢复；
2. 正常新 turn 先按增量方式 append；
3. 若检测到重复率过高，调度后台压实；
4. 压实成功后，705 MB 级重复 JSONL 应回落到“当前 transcript 单份大小 + checkpoint 大小 + 少量 tail”。

任何测试不得复制开发者 `~/.voidx/sessions` 数据；使用合成节点和临时 `VOIDX_HOME`。

### 7.3 P2：UI 事件公平调度与安全合并

#### 7.3.1 协作式让出

`UiEventBus._run()` 增加 batch budget：

- 连续处理最多 32 个 ready event；或
- 连续同步处理达到 4 ms；
- 任一条件满足后执行 `await asyncio.sleep(0)`。

具体数值可依据 benchmark 微调，但必须同时有“数量上限”和“时间上限”。`request()` future、FIFO 和错误传播语义保持不变。

#### 7.3.2 stream update 合并的前置契约

当前 `AssistantStreamUpdated.text` schema 只声明 `str`，没有明确它是累计全文还是增量片段，而 `BottomInputDock.set_stream()` 使用替换语义。因此 P2 实施前必须先建立并测试以下协议：

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

## 8. 文件改动范围

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
| `src/voidx/presentation/adapters/persistence/transcript_adapter.py` | durable turn cursor 与后台压实调度 |
| `src/voidx/persistence/jsonl.py` | 如需要，增加原子 replace records helper；保持 session lock 语义 |
| `src/voidx/agent/adapters/langgraph/runtime/session_runtime.py` | 跟踪 checkpoint task，在 session 生命周期收尾 |
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

### 8.4 基准工具

| 文件 | 责任 |
|---|---|
| `scripts/benchmark_tui_long_session.py`（新建） | 生成匿名合成长会话并测 enter、start turn、stream、snapshot 和 event-loop gap |

## 9. TDD 实施任务

### 9.1 P0 任务

- [ ] **P0.1 建立增量等价测试**：构造 turn、assistant、tool、stream、collapse、remove 序列；先证明当前 root append 会调用 full render。
  - 文件：`src/tests/test_presentation/output/test_tree_incremental.py`
  - RED：root 尾部追加后旧节点被 `_walk_render()` 再次访问。
  - 验证：`./test.py --backend -- src/tests/test_presentation/output/test_tree_incremental.py -v`

- [ ] **P0.2 实现 root tail append**：只渲染新增 root blocks；不变更公共 `render()` 结果。
  - 文件：`src/voidx/presentation/output/tree.py`
  - GREEN：增量 lines/maps/ranges 与 full 完全相等，旧节点访问次数为 0。
  - 验证：同 P0.1。

- [ ] **P0.3 实现 subtree tail splice 和 sibling flag O(1) 更新**。
  - 文件：`src/voidx/presentation/output/tree.py`
  - RED：更新缓存尾部时发生完整 prefix copy/map rebuild；尾部 add_child 遍历全部 siblings。
  - GREEN：操作计数只与 dirty subtree/tail 大小相关。
  - 验证：同 P0.1。

- [ ] **P0.4 thinking stream 改用 node range**。
  - 文件：`src/voidx/presentation/output/dock/app.py`
  - 测试：大 line map 中查询 thinking stream 不遍历历史 key。
  - 验证：`./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_streaming.py -v`

- [ ] **P0.5 TUI flush 接入尾部 cache**。
  - 文件：`tui/voidx_cli/app.py`、`tui/voidx_cli/render_frame.py`
  - RED：10k 节点历史后 start turn 会重新转换旧 lines。
  - GREEN：仅新增 settled lines 进入 `text_from_line()`；回车先清空输入。
  - 验证：`./test.py --backend -- tui/tests/test_frame_advanced.py tui/tests/test_frame_rendering.py -v`

- [ ] **P0.6 resize/reset/restore 回退测试**：确认异常结构变化仍走 full 且结果正确。
  - 验证：`./test.py --backend -- src/tests/test_presentation/output tui/tests/test_frame_advanced.py tui/tests/test_frame_rendering.py -v`

### 9.2 P1 任务

- [ ] **P1.1 写 transcript 增长回归测试**：连续保存 N 轮后，JSONL 中只有一次初始 snapshot 和 N-1 个 turn delta，不出现 N 份完整历史。
  - 文件：`src/tests/test_presentation/adapters/persistence/test_transcript_adapter.py`
  - RED：当前 reset 数随 persist 次数增长，文件呈二次增长。
  - 验证：`./test.py --backend -- src/tests/test_presentation/adapters/persistence/test_transcript_adapter.py -v`

- [ ] **P1.2 实现按 turn 导出和 append transaction**。
  - 文件：`transcript_snapshot.py`、`transcript_adapter.py`
  - GREEN：每轮 rows/bytes 与当前 turn 大小相关；reload 结果与 live tree 等价。
  - 验证：同 P1.1。

- [ ] **P1.3 修正 `replace_transcript()` 为原子覆盖**。
  - 文件：`transcript_snapshot.py`、`jsonl.py`
  - 测试：连续 replace 后 reset 只有一份；模拟 replace 前失败时旧文件仍可读。
  - 验证：`./test.py --backend -- src/tests/test_persistence/test_jsonl_store.py src/tests/test_presentation/adapters/persistence/test_transcript_adapter.py -v`

- [ ] **P1.4 增加 incomplete tail 恢复**。
  - 测试：缺 `turn_end`、损坏尾行、index size mismatch、重复完整 turn。
  - GREEN：只恢复完整事务并重建 index。
  - 验证：同 P1.1。

- [ ] **P1.5 增加 checkpoint/legacy compaction**。
  - 测试：构造多份旧 snapshot，压实后文件显著缩小，恢复结果不变。
  - 验证：同 P1.1。

- [ ] **P1.6 session runtime 端到端回归**。
  - 验证：`./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_session_run_once.py -k "transcript or restore" -v`

### 9.3 P2 任务

- [ ] **P2.1 建立 stream 累计快照 contract test**，覆盖正常文本、thinking、retry/failure 和 commit。
  - RED 条件：任一生产者发送 delta 片段。
  - 在 RED 未解决前不得实现丢帧/coalescing。
  - 验证：`./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_streaming.py -v`

- [x] **P2.2 实现事件 batch budget 和公平让出**。
  - 实现：默认最多 32 个 ready event 或 4 ms；测试可注入 clock，预算后协作式让出，不改变 FIFO/future/drain/stop/异常语义。
  - 测试：预填充大量同步事件，同时运行 probe；probe 在队列 drain 前获得执行，数量预算与时间预算均有确定性覆盖。
  - 验证：`./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_dock_bus.py -v`

- [ ] **P2.3 实现连续同 stream update 合并**。
  - 测试：最终文本不变、barrier 顺序不变、request future 不被合并。
  - 验证：同 P2.2。

- [ ] **P2.4 增加 queue/render/transcript 慢路径指标**。
  - 验证：相关单测 + benchmark 无日志风暴。

### 9.4 本轮补充任务（2026-08-28）

- [x] **TUI 异步 canonical commit**：提交事件立即返回；canonical projection 在线程中构建，安装前校验 node、revision、generation，stale 结果丢弃，worker 异常使用 escaped plain projection 回退。
  - 文件：`src/voidx/presentation/output/dock/stream.py`、`src/voidx/presentation/output/events/consumers.py`、`src/tests/test_presentation/gateway/test_ui_events_streaming.py`
  - 验证：目标 backend 测试 75 passed；相关 presentation + TUI 集合 779 passed。

- [x] **TUI scrollback barrier 与退出顺序（不包含慢 PTY/backpressure）**：`render_pending` stream 不得进入 native scrollback；shutdown 先 drain commit、force flush 和 writer flush，再 restore terminal，最后在线程中导出 `transcript.log`。
  - 文件：`src/voidx/presentation/terminal/run_loop.py`、`tui/voidx_cli/app.py`、`tui/tests/test_input_advanced.py`
  - 验证：退出顺序测试通过；目标 backend 测试 75 passed。

## 10. 性能验收标准

### 10.1 功能测试中的复杂度断言

CI 单测优先断言“旧节点访问次数/旧 line 转换次数为 0”，而不是依赖易波动的绝对毫秒：

- root tail append：旧 `_walk_render()` 节点访问数为 0；
- active subtree tail update：访问范围不超过当前 active subtree；
- thinking stream 查询：不遍历 range 外 line map；
- transcript persist：导出 rows 数等于新增 turn rows，不等于完整树 rows；
- UI event bus：队列未 drain 前 heartbeat 至少运行一次。

### 10.2 本地 benchmark 门槛

标准夹具：10,000 节点、约 50,000 渲染行、100 列、30 行 terminal，历史全部 settled/committed。

| 指标 | 目标 |
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

执行命令：

```bash
./python.py scripts/benchmark_tui_long_session.py \
  --nodes 10000 \
  --rendered-lines 50000 \
  --width 100 \
  --terminal-height 30
```

benchmark 输出必须包含机器信息、Python 版本、节点/行数、每种 render strategy 次数、p50/p95/max 和 transcript bytes。禁止读取真实用户 session。

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

### 11.1 提交顺序

1. benchmark + 复杂度回归测试；
2. P0 OutputTree/TUI；
3. P1 transcript delta/replace；
4. P1 checkpoint/legacy compaction；
5. P2 event fairness；
6. P2 stream coalescing（仅 contract 成立后）。

不得把 P0、P1、P2 合成一个不可独立回滚的大提交。

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
| checkpoint 与 append 并发 | index/offset 不一致 | 同一 session lock；manifest/index 最后提交 |
| crash 留下半个 turn | 恢复出不完整 UI | `turn_end` 事务边界；异常 tail 丢弃 |
| 后台 JSON 编码竞争 GIL | 输入仍有抖动 | 降低 checkpoint 频率、仅 idle 调度、监控 heartbeat gap；必要时再评估进程池 |
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

## 14. 完成定义

全部满足才可将本文档从 `docs/design/` 推进为已实现状态：

1. P0/P1 focused tests、presentation + TUI 回归和完整 backend 全绿；
2. 10k 节点 benchmark 达到第 10 节门槛；
3. 增量 render 与 full render 的 lines/maps/ranges 差分测试覆盖所有支持的 mutation；
4. 连续 100 turn 合成 transcript 不再出现二次增长；
5. 旧版重复 transcript 可在后台压实且恢复结果一致；
6. event bus 压力测试中输入/heartbeat 不被 drain 循环饿死；
7. 未建立 stream snapshot contract 时，P2 coalescing 保持未启用；
8. 最终验证后，按项目规则将完成的 spec/archive 文档归档，而不是提前归档本设计。
