> **Status: Done** — Archived on 2026-09-06.

---
name: tui-dynamic-layout-snapshot-2026-09-04
display_name: TUI Dynamic Layout Snapshot and Targeted Refresh
description: 统一 TUI 动态区域几何快照，并在生产 worker 路径以定点行刷新替代普通更新中的整屏清除
doc_type: tech-design
audience: human+llm
status: review
---

# TUI 动态布局快照与定点刷新 — 修订设计稿

## TL;DR

本阶段建立一条纯布局链路：

```text
LogicalRenderPlan → PhysicalViewportPlan → LayoutSnapshot/LayoutDiff → sync/worker adapter
```

TUI 为 transcript、global pinned todo、vibe/activity、thinking 和强制组合的 bottom 区域建立统一快照。bottom 拥有全部 separator，并保存 input、panel、status 的子几何和 cursor geometry。

生产 TTY 始终使用 `TerminalWriter` worker，因此本阶段必须同时修改 writer 的内部 frame diff，不能把 worker 永久回退到 full frame。公共 `FrameBatch`、`submit_frame()`、`submit_commit()` 和 barrier API 保持不变；worker 继续接收完整目标行，但在 baseline 可信时只写变化行或变化 suffix，旧尾逐行 `EL`，普通更新不使用 `CSI J`。

刷新规则：

- 内容变化且区域高度不变：只 patch 变化区域；
- 区域高度、宽度或锚点变化：从最早变化区域重绘到 bottom 末尾；
- clear、resize、显式 scroll、restore、commit、writer error 或旧几何不可用：走完整物理 viewport frame，并使旧绝对锚点失效；
- 当前 retained/uncommitted transcript 先生成不受 terminal height、`body_limit`、`tail_limit` 或 `node_count >= 256` 影响的完整逻辑 plan，再投影为不超过物理终端高度的 viewport；投影不会修改 tree/payload，settled 内容仍只由现有 commit lane 完整追加到 scrollback；
- 只有 `FrameResult(applied=True)` 对应的 generation 才能成为 renderer 的 applied viewport snapshot，排队中的布局只能保存在 pending snapshots；worker 是否执行行 diff 只由其私有 applied baseline 决定；
- 普通局部刷新只使用绝对定位和逐行 `CSI K`/`EL`，不得使用 `CSI J`。

Todo 保留现有两种显示策略：global pinned 使用 4 行 body budget，ellipsis 占其中一行；subagent 使用最多 8 个 item，ellipsis 是额外一行。两者共享状态规范化、稳定排序、遗漏计数、ellipsis 生成、cell-width 裁剪和最终视觉行解析算法，但通过 policy 配置不同预算、前缀、有效宽度和样式。显示投影不得删除完整 todo payload 或 tree 数据。

## 1. 当前基线

### 1.1 现有渲染链

- `tui/voidx_cli/render_frame.py` 组装 transcript、pinned todo、busy activity、thinking 和 bottom，并同时存在完整 frame、input-only 和 activity-only 刷新路径。
- `tui/voidx_cli/render_todo.py` 渲染 global pinned todo；`_TODO_PINNED_MAX_ITEMS = 4` 实际表示 header 之外的 4 行 budget。超过 budget 时 ellipsis 占一行，因此最多显示 3 个 item 加 ellipsis。
- `src/voidx/presentation/output/dock/todo.py` 渲染 subagent todo；`TODO_MAX_VISIBLE_ITEMS = 8` 表示最多显示 8 个 item，遗漏时额外追加 ellipsis。
- `src/voidx/presentation/output/events/consumers.py` 当前在 `_upsert_subagent_todo_node()` 中将 subagent todo 转成 tree node 的 header/body/payload。
- `tui/voidx_cli/render_activity.py` 渲染 vibe/activity；权限详情和 loop waiting 可能产生多行。
- `tui/voidx_cli/render_input.py` 计算输入折行和光标。
- `tui/voidx_cli/render_status.py` 生成 status bar。
- `tui/voidx_cli/state.py` 保存 frame、bottom、input、busy activity 和 commit 的分散缓存。
- `tui/voidx_cli/app.py` 在正常 TTY 启动时必定启动 `TerminalWriter` worker。
- `tui/voidx_cli/terminal_writer.py` 合并队尾 frame，以 synchronized output 写 worker frame；当前超过 80% 行变化或旧尾缩短时会进入使用 `CSI J` 的 full/tail-clear 路径。

### 1.2 已知问题

1. 区域几何分散在 `_last_*` 字段和 `_RenderPlan`，没有统一的旧/新布局比较。
2. `_render_input_region()` 捕获整个 bottom，并用 `CSI J` 清理到屏幕末尾。
3. `_render_busy_activity_tick()` 使用独立几何字段；worker 模式直接回退 `_render_frame()`。
4. worker 模式在 `submit_frame()` 后立即更新 renderer 缓存，但 frame 可能仍在排队、被新 frame 合并替换，或被 commit/barrier 丢弃。
5. `_rendered_row_count()` 使用 `count("\n") + 1`，部分写入路径使用 `splitlines()`；有意义的末尾空行会出现计数与行列表不一致。
6. bottom 实际包含多个 separator，但旧设计没有规定 separator 的唯一归属。
7. global/subagent todo 共享排序目标，但现有 4/8 上限的 ellipsis 预算语义不同；subagent 的最终有效宽度还受 tree prefix 影响。
8. 当前动态 frame 可能高于物理终端。现有路径用 `body_limit`/`tail_limit` 截断 transcript，或让超高 frame 自然滚屏；前者会从显示层丢掉逻辑行，后者会在后续 input/timer 刷新时把同一 transcript 重复写入 scrollback。

## 2. 目标与非目标

### 2.1 目标

- 用不可变 `LayoutSnapshot` 描述一次目标 frame 的区域行、绝对几何、终端尺寸、scroll epoch 和 cursor。
- 以唯一 `RenderedRows` 规范生成行列表、视觉行数、内容签名和完整 `FrameBatch.target_lines`。
- 固定顶层区域顺序：

  ```text
  transcript
  global pinned todo
  vibe/activity
  thinking
  bottom
  ```

- 强制 bottom 作为父区域，拥有 top separator、input、middle separator、panel、panel/status separator 和 status；input/panel/status 同时保存子几何。
- 内容同高时只更新变化行；高度变化时从最早变化区域更新 suffix。
- 生产 worker 与同步兼容模式都执行同一份布局 diff 语义；二者只在提交 adapter 上不同。
- worker 只将真实 applied generation 提升为绝对定位 baseline；pending、coalesced、invalidated 和 stale generation 不能污染 cursor 或下一次 diff。
- 普通局部刷新和 worker 的可信 baseline diff 不使用 `CSI J`；旧尾逐行发送 `CSI K`。
- 明确定义局部 patch 的物理行安全条件；逻辑 plan 不按高度截断或删除，物理 `FrameBatch.target_lines` 始终是可安全锚定、行数不超过 terminal height 的 viewport 投影。
- 保留 todo 完整 payload、parent 生命周期和现有 4/8 显示策略，同时复用同一规范化与投影算法。
- 将几何/diff 与提交 adapter 分离，使后续 terminal scheduler 只替换提交层，不重写布局算法。

### 2.2 非目标

- 不实现 UI commit lane、`UiEventBus.drain()` pending projection、stream commit happens-before 或 LangGraph 生产顺序修复。
- 不实现 `OutputTree.detach_completed_prefix()`、提交后节点释放，或移除 root turn/字节历史淘汰。
- 不修改 `TerminalWriter` 的公共 `FrameBatch`、`FrameResult` 字段、`submit_frame()`、`submit_commit()`、barrier 或 payload 生命周期 API；允许修改其内部 frame diff、baseline 和 result 发布时机。
- 不把 region patch 设计成新的 writer 公共提交协议。worker adapter 仍提交完整 `FrameBatch.target_lines`。
- 不把 todo 显示改成无限，也不以 terminal height、transcript body budget 或 panel budget 删除 todo state/payload/tree 数据。
- 不改变 committed watermark、已提交 scrollback 或既有 root-turn/字节 retention 语义；完整逻辑 plan 只包含当前 retained 且尚未提交的 tree 行和当前动态区域，不重新回放已提交历史。
- 不在 frame renderer 中把 viewport 省略行写入 scrollback；稳定内容仍由现有 `_flush_committed()`/`submit_commit()` 路径恰好提交一次。
- 不把 subagent todo 复制到 global pinned 区，也不把 vibe/activity 写入 transcript scrollback。
- 不改变 status、panel、input 的业务内容。
- 不在本阶段彻底消除显式 clear、restore、commit 或 baseline 不可信时完整重绘所使用的 `CSI J`。

## 3. 渲染行与布局模型

### 3.1 `RenderedRows`

所有区域必须先经过同一个纯规范化函数生成：

```text
RenderedRows
  ansi: str
  rows: tuple[str, ...]
  visual_rows: int
  signature: tuple
  patch_safe: bool
```

规范如下：

1. Rich capture 只移除 `Console.print()` 自己添加的一个末尾 `\n`；业务内容产生的尾部空行必须保留。
2. 空字符串映射为 `rows=()` 和 `visual_rows=0`。
3. 非空内容只按字面 `\n` 拆分，必须保留尾部空字符串。例如规范化后的 `"line\n"` 映射为 `("line", "")`，不能使用会丢失该空行的 `splitlines()`。
4. Rich 折行必须在区域的最终有效宽度下完成。逻辑 item 数、renderable 数或 `len(elements)` 不能代替 `visual_rows`。
5. 行内允许不改变光标位置的样式控制，例如 SGR 和 Rich 产生的 OSC 8 链接；若捕获结果含 `\r`、cursor movement、erase、scroll 或其他会改变布局的控制序列，则 `patch_safe=False`，本次不得局部 patch。
6. `signature` 来自规范化后的行、有效宽度和影响投影的 policy，不依赖 Rich 对象 identity。
7. `LogicalRenderPlan` 的每个 source region 和 `PhysicalViewportPlan` 的每个 projected region 都使用本规范；`FrameBatch.target_lines` 只能由 projected regions 连接得到，不得再用另一套 `splitlines()` 重新解析。
8. 开发期加入一致性断言：source regions 重组后的可见文本等于完整逻辑 Group capture；projected regions 重组后等于 `FrameBatch.target_lines`。任一不一致都标记 capture/layout 失败，不猜测区域边界。

这套结果同时是视觉行数、区域边界、diff 输入和 writer target 的单一事实来源。

### 3.2 `RegionGeometry`

```text
RegionGeometry
  key: str
  start_row: int              # 1-based；缺席区域取其逻辑插入点
  visual_rows: int            # 0 表示缺席
  width: int                  # 该区域实际 capture width
  content_signature: tuple
  patch_safe: bool
```

`RegionGeometry` 不保存 generation。generation 用于关联提交，不参与内容或几何 equality，否则每次渲染都会被误判为变化。

### 3.3 bottom 父区域

separator 必须有唯一所有者，不能作为实现可选项。bottom 固定为父区域：

```text
BottomGeometry
  rendered: RenderedRows
  region: RegionGeometry
  top_separator: RegionGeometry
  input: RegionGeometry
  middle_separator: RegionGeometry
  panel: RegionGeometry
  panel_status_separator: RegionGeometry
  status: RegionGeometry
  cursor_row: int       # absolute 1-based physical row
  cursor_col: int       # 1-based terminal column
```

规则：

- `bottom.rendered.rows` 是 projected 子区域按 `top_separator → input → middle_separator → panel → panel_status_separator → status` 连接后的连续物理行；`bottom.region` 记录其整体几何并拥有完整 bottom signature。
- panel 缺席时，`panel.visual_rows=0`，`panel_status_separator.visual_rows=0`；没有孤立 separator。
- panel 出现或消失时，从第一个变化的 bottom 子区域生成 bottom suffix；顶层比较仍把 bottom 当作一个连续父区域。
- source rows 可以被 viewport 选成多个不连续片段，但 projected rows 必须始终按上述顺序连续连接；cursor 只能由本次 bottom 子几何计算，不能从 `_last_bottom_rows`、panel row count 和 status count 临时拼接。

#### 3.3.1 `BottomViewportPlan`

bottom 的业务源状态与物理投影分离：

```text
BottomViewportPlan
  source_signature: tuple
  source_children: tuple[tuple[str, RenderedRows], ...]
  projected: BottomGeometry
  projected_slices: tuple[SourceSlice, ...]
  source_cursor_key: str
  source_cursor_row: int             # 0-based within source child
  projected_cursor_row: int          # 0-based within projected bottom
  omitted_input_before: int
  omitted_input_after: int
  panel_omitted: bool
  status_omitted_rows: int
```

`source_children` 按 `top_separator → input → middle_separator → panel → panel_status_separator → status` 保存完整 source child；零行 child 不进入 tuple。`projected_slices` 按 projected 物理顺序保存一个或多个 `SourceSlice`，因此 input window、panel selected row/context、status 和 separator 的来源与投影位置都可独立追踪。

投影算法是确定性的：

1. 先按完整 input、panel 和 status state 生成 source children，不修改 `_input_lines`、panel matches/selection、notice 或 error。
2. 若 source bottom 不高于 terminal height，所有 source children 原样进入 projected；每个 child 生成一个连续 slice。
3. 若超高，先保留包含 input cursor 的连续 input window，这是唯一强制可见内容。剩余空间按固定优先级依次扩展：selected panel row、其相邻 panel context、primary status、其他 status；同一优先级按 source 顺序稳定选择。input window 即使只保留一行也必须包含 cursor。
4. separator 只有在其相邻的内容 child 都被选中时才进入 projected；separator 的 owner 始终是 bottom，不占用其他子区域的 source slice。最终 projected children 必须仍按固定 bottom 顺序连接。
5. 所有 source/projected 行索引均为 0-based、左闭右开；`RegionGeometry.start_row`、`BottomGeometry.cursor_row` 和 `LayoutSnapshot.cursor_row` 均为绝对 1-based 物理行。若 source cursor 为 `(source_cursor_key, source_cursor_row)`，且对应的一个 `SourceSlice` 把该 child 的 `[source_start, source_end)` 映射到 projected bottom 的 `[projected_start, projected_end)`，则：

   ```text
   projected_cursor_row = projected_start + source_cursor_row - source_start
   cursor_row = projected.region.start_row + projected_cursor_row
   ```

   `cursor_col` 直接使用 input renderer 计算的 1-based 列；实现必须断言 cursor source row 恰好落在一个 projected slice 内。
6. `projected` 必须满足 `1 <= visual_rows <= terminal_height` 且 projected cursor 位于其中；否则是 layout error，不能提交 frame。
7. `LayoutSnapshot.bottom` 和 frame target 使用 projected rows；`source_signature` 只用于确认投影由当前业务状态生成，不参与终端行 diff。

### 3.4 逻辑计划与物理 viewport

```text
LogicalRenderPlan
  source_regions: tuple[RenderedRows, ...]
  bottom_source: BottomViewportPlan
  source_cursor: tuple[str, int]  # (bottom child key, 0-based row)
  source_signature: tuple

PhysicalViewportPlan
  projected_regions: tuple[RenderedRows, ...]
  bottom: BottomGeometry
  source_slices: tuple[SourceSlice, ...]
  frame_rows: int
  cursor_row: int
  cursor_col: int
  source_signature: tuple

SourceSlice
  key: str                 # top-level key or bottom.<child-key>
  source_start: int        # 0-based, inclusive within source child
  source_end: int          # 0-based, exclusive
  projected_start: int     # 0-based, inclusive within projected region
  projected_end: int       # 0-based, exclusive
  mode: full | compact | tail
```

`source_slices` 按物理输出顺序排列且不得重叠。顶层 slice 的 `projected_*` 相对于该顶层 projected region；`bottom.<child-key>` slice 的 `projected_*` 相对于 projected bottom。`full`/`tail` slice 保留逐行来源映射，`compact` slice 只记录 renderer 定义的 provenance，不宣称 compact 行与 source 行同一索引；cursor 映射只允许使用包含 cursor 的逐行 slice。

`LogicalRenderPlan` 由完整 retained/uncommitted tree 行、完整 todo policy 投影及当前 vibe/thinking/bottom 源状态组成，不受 terminal height 影响。`PhysicalViewportPlan` 才应用 terminal height：

1. 先通过 `BottomViewportPlan` 得到包含 input cursor 的 bottom 投影。
2. 剩余行用于 `thinking → vibe → todo` 的 compact/expanded 投影，最后用于 transcript tail；最终组装仍保持 `transcript → todo → vibe → thinking → bottom`。
3. todo compact row 是 header；vibe compact row 是 activity 主行；thinking compact row 是当前 thinking 主行。每个 renderer 同时提供完整 rows、compact rows 和确定的 expansion order，不允许 viewport 层猜测字符串语义。
4. transcript 只取 source tail；`SourceSlice` 记录每个 projected region 对应的 source `[start, end)`，不得修改 tree、payload、committed watermark 或业务 state。
5. `frame_rows <= terminal_height` 且 cursor 必须位于 projected bottom；否则是 layout error，不提交 frame。
6. viewport 省略的 settled tree 行仍由 `_flush_committed()` 基于完整 `dock.tree.render_with_line_map(width)` 提交；frame renderer 永不把省略行写入 scrollback。

### 3.5 `LayoutSnapshot`

```text
LayoutSnapshot
  terminal_width: int
  terminal_height: int
  frame_start_row: int
  frame_rows: int
  regions: tuple[RegionGeometry, ...]  # projected transcript/todo/vibe/thinking/bottom
  source_slices: tuple[SourceSlice, ...]
  bottom: BottomGeometry
  cursor_row: int
  cursor_col: int
  scroll_epoch: int
  generation: int
```

snapshot 只描述已提交或待提交的物理 viewport，不把未显示 source 行伪装成终端几何。区域绝对行从 `frame_start_row` 向下单向累加；每份合法 snapshot 都满足：

```text
1 <= frame_start_row
frame_rows <= terminal_height
frame_start_row + frame_rows - 1 <= terminal_height
1 <= cursor_row <= terminal_height
```

`generation` 只关联 `FrameBatch`、pending snapshot 和 `FrameResult`，并用于忽略 stale callback、清理旧 pending；它不参与 region 内容/几何 equality。`scroll_epoch` 表示绝对锚点世代，clear、resize、显式 scroll、restore、commit 或 writer error 会使旧 epoch 失效。

### 3.6 `LayoutDiff` 与 adapter

纯 diff 层比较两份物理 viewport snapshot，输出：

```text
LayoutDiff
  kind: unchanged | cursor | regions | suffix | full
  first_region: str | None
  first_absolute_row: int | None
  changed_rows: tuple[int, ...]
  old_tail_rows: tuple[int, ...]
  reason: str
```

提交 adapter：

- sync adapter 将 `LayoutDiff` 编码为单个 ANSI patch payload，经现有 `TerminalWriter.write()`/`flush()` 提交；
- worker adapter 始终提交完整的物理 viewport `FrameBatch.target_lines`；`force_full` 只表达 resize/clear/显式 scroll/commit/restore/error、start-row 变化或其他明确 baseline invalidation，不比较 renderer callback 是否已处理；worker 内部只根据自己的 applied frame baseline 决定精确 diff；
- renderer、timer 和 input handler 不直接写 `sys.stdout`。

## 4. Todo 设计

### 4.1 共享规范化与差异配置

在 `src/voidx/presentation/output/dock/todo.py` 定义：

```text
TodoRenderPolicy
  max_visible_items: int | None
  body_row_budget: int | None
  ellipsis_consumes_budget: bool
  include_item_id: bool

TodoRenderPlan
  summary: str
  ordered_items: tuple[DockTodoItem, ...]
  visible_items: tuple[DockTodoItem, ...]
  omitted_count: int
  has_ellipsis: bool
  logical_lines: tuple[TodoRenderLine, ...]
  payload_signature: tuple
```

共享算法：

1. 按 `active → pending → done` 稳定排序；未知状态按输入顺序置于已知状态之后，并使用 pending 风格 fallback，不得从投影中静默丢失。
2. 根据 policy 计算 `visible_items`、`omitted_count` 和 ellipsis 是否占 budget。
3. 统一生成 item/ellipsis 的未裁剪结构化逻辑行；样式、icon 和 id 显示是 policy 或位置 renderer 的 decoration。
4. 位置 renderer 使用同一 cell-width 裁剪函数，并从实际终端宽度扣除当次真实 prefix cell width；不得按 Python 字符数裁剪 CJK、emoji 或组合字符。
5. 完整原始 items 始终保存在 `DockTodoState` 和 node payload。`visible_items` 只是显示投影。
6. global 在 pinned renderer 中裁剪；subagent 在每次 `OutputTree.render(width)` 时从 node 的完整 todo payload 重新生成 tree-prefix-aware 投影。consumer 不保存按某一终端宽度裁剪后的永久 body。
7. 最终 `visual_rows` 由各自真实 renderer 在实际宽度、缩进和 tree prefix 下生成 `RenderedRows`；不能直接复用另一位置的行数。

保留现有策略：

| 位置 | policy | 现有可见语义 |
|---|---|---|
| global pinned | `body_row_budget=4`, `ellipsis_consumes_budget=True`, `include_item_id=False` | 不遗漏时最多 4 item；遗漏时最多 3 item + ellipsis |
| subagent tree | `max_visible_items=8`, `ellipsis_consumes_budget=False`, `include_item_id=True` | 最多 8 item，遗漏时额外一行 ellipsis |

“共享 render plan”指共享同一 builder 和行生成算法，不表示两个位置必须使用相同预算数值、prefix 或最终宽度。

### 4.2 Global pinned todo

- 来源：`dock.todo_state()`。
- 位置：transcript 后、vibe/activity 前。
- `tui/voidx_cli/render_todo.py` 消费共享 plan，仅负责 pinned header、style、icon 和有效宽度配置。
- terminal height 不再二次缩小 todo policy；物理空间不足由 frame 安全/fallback 规则处理，不修改 plan 或 payload。
- 同高内容变化只更新 todo 行；高度变化、出现或清除时从 todo 开始更新 suffix。

### 4.3 Subagent todo

- 来源与生命周期仍由 `DockEventConsumer._upsert_subagent_todo_node()` 和 `_clear_subagent_todo_node()` 管理。
- consumer 使用共享 builder 保存完整 payload，并写入不依赖终端宽度的兼容 header/body；不能继续复制排序和 ellipsis 逻辑。
- `OutputTree` 的 todo 分支在 `render(width)` 时从 payload 调用共享 builder，再按当前 branch prefix 和有效 cell width 生成行；resize 不复用旧宽度下的 todo 文本。
- tree renderer 继续拥有 branch prefix、缩进和最终 tree 宽度；subagent todo 只影响 transcript 区域的最终 `RenderedRows`，不创建独立 pinned geometry。
- 完成 snapshot、清除和 parent settle 语义保持不变。

## 5. Diff、物理安全与写入算法

### 5.1 局部 patch 资格

只有同时满足以下条件，`LayoutDiff` 才能是 `cursor`、`regions` 或 `suffix`：

1. 旧 applied viewport snapshot 存在，且与新 snapshot 属于同一 `scroll_epoch`。
2. 新旧 terminal width/height 和 frame start 相同。
3. 新旧 snapshot 均满足 §3.5 的 frame/cursor 物理行不变量。
4. 全部待写行和旧尾清理行都位于 `1..terminal_height`。
5. 所有受影响 `RenderedRows.patch_safe=True`。
6. 没有未决 commit、clear、显式 scroll、resize、restore 或 invalidating barrier。
7. sync baseline 已成功 flush。

这些资格只决定 renderer 的 sync region/suffix patch。worker adapter 不因 `FrameResult` callback 尚未在事件循环执行、renderer applied generation 落后或存在普通 pending frame而设置 `force_full`；worker 是否 diff 由 worker线程自己的 `_baseline_valid`、`_applied_start_row` 和 `_applied_lines` 决定。

任一条件不满足即提交完整的**物理 viewport** frame。frame 本身不会自然滚屏；若 `visible_committed_rows + frame_rows > terminal_height`，先用现有 scroll barrier 仅滚走所需的已提交可见行，递增 `scroll_epoch`，再以 `force_full=True` 在新 `frame_start_row` 写入 viewport。

### 5.2 比较规则

按顶层 `transcript → todo → vibe → thinking → bottom` 比较：

1. snapshot 缺失、终端尺寸/frame start/scroll epoch 不同或 patch 不安全：full。
2. 第一个 `start_row` 或 `visual_rows` 不同的顶层区域：从该区域到 bottom 末尾重绘 suffix。
3. 顶层几何相同，仅 content signature 不同：只 patch相应区域的变化行。
4. 只有 bottom signature 变化时，再按 bottom 子区域顺序定位最早变化行；separator 属于 bottom，不会遗漏。
5. 仅 cursor 变化：只提交 cursor 定位。
6. generation 不参与内容或几何比较。

### 5.3 普通行 patch

单区和 suffix 最终都编码为有序绝对行写入：

```text
BEGIN_SYNCHRONIZED_OUTPUT
CSI <row>;1H
<new row>
CSI K
...
CSI <old-tail-row>;1H
CSI K
...
CSI <cursor-row>;<cursor-col>H
END_SYNCHRONIZED_OUTPUT
```

规则：

- 每个新行后发送 `CSI K`，避免短内容留下旧尾。
- 新 suffix 比旧 suffix 短时，对每个多余旧行分别绝对定位并发送 `CSI K`。
- 普通 patch 不发送 `CSI J`，也不因变化比例超过 80% 自动升级到 full。
- sync adapter 预先构造完整 payload，以 synchronized-output begin/end 包裹；即使 `TerminalWriter.write()` 因 byte budget 分段 drain，正常路径也只在 end 后展示完整更新。
- `write()` 或 `flush()` 抛错时不得继续渲染或发布 snapshot：先使 layout baseline 失效，再调用 `TerminalWriter` 的私有 best-effort recovery；该方法丢弃尚未写出的 patch buffer，通过 writer 自己的底层写入路径发送 `CAN (\x18) + _END_SYNCHRONIZED_OUTPUT + _EXIT_TERMINAL_SEQUENCE` 并 flush，以取消可能写到一半的控制序列和退出 synchronized-output。随后将 writer/TUI 标为失败并进入现有 terminal lifecycle cleanup。
- best-effort recovery 成功也不恢复本轮 frame 的 applied 状态；若 recovery 再失败，同时记录原始错误与恢复错误，并停止所有后续 terminal write。所有恢复字节仍经 `TerminalWriter` 路径，不直接写 `sys.stdout`。

### 5.4 worker 内部精确 diff

保持 `FrameBatch` API 不变，修改 `TerminalWriter._write_frame_diff()`：

- 删除“变化行超过 80% 就 full render”的阈值；baseline 可信时无论比例都逐行写变化。
- `current` 短于 `previous` 时逐行 `CUP + CSI K` 清除全部旧尾，不发送 `CSI J`。
- 保留 `_process_frame()` 的 synchronized-output 包裹。
- 只有 `force_full=True`、worker 私有 baseline 无效或 start row 改变时调用 full renderer；callback 尚未执行和 renderer generation 落后不是 full 条件。
- full renderer、显式 clear、restore 和 commit 可继续使用既有完整清理语义。
- frame target 始终满足 `len(target_lines) <= terminal_height`；由 commit 或显式 scroll 改变 viewport 锚点时，renderer 在 barrier 前失效 layout，barrier 后的下一 frame 使用 `force_full=True`。
- frame 写入和 flush 成功后才发布 `FrameResult(applied=True)`；失败时走现有 error callback，并使 renderer/worker baseline 同时失效。

### 5.5 applied/pending snapshot 状态机

`RenderState` 保存：

```text
applied_layout_snapshot: LayoutSnapshot | None
pending_layout_snapshots: dict[int, PendingLayoutSubmission]
pending_terminal_operations: dict[int, PendingTerminalOperation]
layout_generation: int
submitted_generation: int
scroll_epoch: int
full_layout_invalidated: bool
terminal_submission_failed: bool

PendingLayoutSubmission
  snapshot: LayoutSnapshot
  scroll_epoch: int
  force_full: bool

PendingTerminalOperation
  kind: barrier | commit
  token: BatchToken
  scroll_epoch: int
  apply_state: Callable | None
```

worker 模式：

1. 分配 generation `N`，构造 `PendingLayoutSubmission` 并写入 `pending_layout_snapshots[N]`，再在 `try` 中提交相同 generation 的 `FrameBatch`。当 `full_layout_invalidated` 为 true 时，该提交必须带 `force_full=True`，并将 `force_full` 记录在 pending entry；renderer 不得因 callback 尚未执行而清除该标记。
2. 若 `submit_frame()` 抛错，立即删除 pending `N`，调用幂等的 `handle_terminal_submission_failure("frame_enqueue", error)`：设置 `terminal_submission_failed`，调用 `invalidate_layout`，递增 `scroll_epoch`，停止后续提交并进入现有 terminal cleanup；`layout_generation=N` 已消费且永不复用，`submitted_generation` 不变。
3. 若提交返回成功，将 `submitted_generation=N`；不得立即覆盖 applied snapshot、cursor baseline 或 `full_layout_invalidated`。
4. 收到 `FrameResult(generation=N, applied=True)`：仅当 pending snapshot 存在且其 `scroll_epoch` 仍等于当前 epoch 时，将它提升为 applied；若该 entry 的 `force_full=True` 且 `full_layout_invalidated` 仍为 true，则此时才清除 `full_layout_invalidated`。随后删除所有 `generation <= N` 的 pending snapshots。callback 提前、延迟或重复到达都不能清除该标记。
5. 收到 `applied=False`：删除该 generation；不得提升 snapshot，也不得清除 `full_layout_invalidated`。
6. writer 队尾 coalescing 可能不会为被替换 frame 发布 result。更高 generation applied 时统一清理更老 pending；只有真实 applied 的当前 epoch 完整 frame 才能清除 full invalidation，因此不会把未写入 frame 当 baseline。
7. 提交 commit 或 invalidating barrier 前，先递增 `scroll_epoch`、清空 applied/pending snapshot 并设置 `full_layout_invalidated=True`；再登记返回的 token operation。barrier/commit 成功只表示其 payload 已完成，不清除 full invalidation，下一 frame 仍必须 full。旧 in-flight frame 的 callback 稍后到达时因 epoch 不匹配而被忽略。
8. writer error、shutdown/restore 失败同样清空 applied/pending snapshot，设置 `full_layout_invalidated=True`，并通过统一失败入口停止后续 terminal write。
9. callback 只处理 renderer snapshot、invalidation 状态和统计，不决定 worker 已经执行的 frame diff，也不触发 stdout 写入。

所有 frame、barrier 和 commit 提交都遵循同一事务协议：

- 同步入队失败（包括 `submit_frame()`、`submit_barrier()`、`submit_commit()` 抛错）必须删除本次 pending entry、调用 `handle_terminal_submission_failure(operation, error)`，不应用任何对应业务状态；writer 的 commit payload 释放仍由 writer 自己负责。
- `submit_barrier()`/`submit_commit()` 返回 token 后，立即登记 `PendingTerminalOperation`，再由 waiter 等待；token 成功前不得调用 `apply_state()`、增加 `visible_committed_rows`、清理 layout invalidation 或恢复 cursor baseline。成功后才应用对应 commit/barrier 的 renderer state，并触发下一次统一 render。
- token 失败（worker 写入、flush、barrier 或 commit payload 处理失败）与 writer error 走同一 `handle_terminal_submission_failure`：先移除失败 operation，清空 applied/pending snapshot，递增 epoch，设置 `full_layout_invalidated=True` 和 `terminal_submission_failed=True`，停止后续提交并进入 terminal cleanup。commit 特有的 guidance echo 恢复在该失效之后执行，且不能把失败 commit 当作已提交。
- `handle_terminal_submission_failure` 必须幂等；同一错误可能同时由 token waiter 和 writer error callback 报告，但只能执行一次 cleanup/停止转换。取消 shutdown waiter 不得伪装成成功，也不得重新启用 writer。

sync 模式没有 pending frame 队列；patch/full payload 成功 flush 后直接发布 applied snapshot。若 `submit_frame()`、write 或 flush 失败，按相同失败入口处理并保持 `full_layout_invalidated=True`。同步模式只有当前 epoch 的完整 payload 成功 flush 后才能清除 `full_layout_invalidated`；局部 patch 成功不能清除它。

### 5.6 timer、输入和 cursor

- `_render_after_input()`、choice/panel 更新和 `_render_busy_activity_tick()` 都调用统一 render/diff 入口，不再维护独立区域写入算法。
- worker 模式不再以“调用 full `_render_frame()`”作为 timer 特例；统一入口生成完整物理 viewport target，由 worker 私有 applied baseline 精确 patch。
- cursor 使用 snapshot 的绝对 `cursor_row/cursor_col`；不再通过“从 frame end 向上若干行”拼接多个旧计数。
- `BottomViewportPlan` 保证 projected bottom 不高于 terminal height 且包含 input cursor visual row；因此 `FrameBatch.cursor_ansi` 使用绝对 `CUP(cursor_row, cursor_col)`，且必须满足 `1 <= cursor_row <= terminal_height`。
- 若断言失败，不发送越界 CUP，不继续交互式渲染；将其视为 layout error，失效 baseline 并进入 writer/terminal cleanup。不得把 cursor 留在错误位置后继续接受输入。
- 当前没有 applied snapshot 时不能执行 cursor-only patch；可随完整物理 viewport frame 一起提交 cursor。

## 6. Full frame、viewport、滚屏与失效

### 6.1 Full frame 条件

以下情况必须 `force_full=True`：

- 首次 frame；
- resize、clear、显式 scroll、restore、alternate-screen 状态切换；
- commit 改变动态 frame 锚点；
- terminal width/height、frame start 或 scroll epoch 变化；
- snapshot/capture 缺失，区域含布局控制序列，或旧 writer baseline 不可信；
- frame、cursor、待写行或待清行越出物理终端；
- writer error 后恢复。

完整物理 viewport frame 和显式 clear/commit 可使用既有 `CSI J`。禁止 `CSI J` 只约束可信 baseline 下的普通 frame diff、region patch 和 suffix patch。

### 6.2 超高逻辑内容

逻辑输出不得因物理高度被删除，但动态 frame 也不得把完整逻辑内容重复写入 scrollback：

- `LogicalRenderPlan` 保留完整 retained/uncommitted tree 行、完整 todo policy 投影和当前 vibe/thinking/bottom 源状态；
- `FrameBatch.target_lines` 只包含 `PhysicalViewportPlan` 投影，且 `len(target_lines) <= terminal_height`；
- viewport 省略的 settled tree 行仍由 `_flush_committed()` 基于完整 `dock.tree.render_with_line_map(width)` 恰好提交一次；
- 若 `visible_committed_rows + frame_rows > terminal_height`，先用现有 scroll barrier 只滚走已提交可见行，递增 `scroll_epoch`，再以 `force_full=True` 写入新 viewport；
- 动态 frame 本身不自然滚屏，也不发布无法映射到当前 viewport 的 snapshot。

本阶段不通过裁剪 todo payload、tree 节点或逻辑 transcript 来伪造可锚定 frame。

### 6.3 统一失效入口

clear、reset、resize、restore、scroll plan、commit 提交和 writer error 必须调用同一 `invalidate_layout(reason, advances_scroll_epoch=True)`。该入口：

- 清空 applied snapshot；
- 清空 pending snapshots；
- 递增 scroll epoch；
- 设置 `full_layout_invalidated=True`，标记下一 frame `force_full`；
- 清除仅用于兼容的旧区域几何缓存。

`full_layout_invalidated` 是 renderer 对绝对锚点的保护状态：提交入队成功、普通 frame callback、`applied=False`、barrier/commit token 尚未成功或失败恢复均不能清除它。worker 只有在当前 epoch 的 `FrameResult(applied=True)` 对应 pending entry 明确要求 `force_full=True` 时才能清除；sync 只有完整 payload 在当前 epoch 成功 flush 后才能清除。

非布局业务内容变化不调用该入口，只生成新 plan 与 diff。

## 7. 状态与兼容迁移

以下旧字段暂时保留给未迁移调用方，但新 diff/cursor 代码不得读取它们拼装几何：

- `last_busy_activity_rows`
- `last_busy_activity_start_row`
- `last_busy_activity_width`
- `last_busy_activity_term_height`
- `last_busy_activity_bottom_rows`
- `last_busy_activity_thinking_rows`
- `last_bottom_rows`
- `last_bottom_start_row`
- `cursor_to_frame_top_lines`
- `cursor_to_frame_end_lines`

迁移完成后，timer、input、choice、panel、status 和 cursor 只读取 `LogicalRenderPlan`/`PhysicalViewportPlan`、applied snapshot 或 pending generation，不再各自判断 `_last_*` 是否匹配。

## 8. 文件结构与实施任务

### 8.1 文件职责

| 文件 | 变更职责 |
|---|---|
| `src/voidx/presentation/output/dock/todo.py` | 定义 `TodoRenderPolicy`/`TodoRenderPlan` 和共享排序、预算、ellipsis、逻辑行算法。 |
| `src/voidx/presentation/output/events/consumers.py` | subagent todo 保存共享 plan 的完整 payload 和宽度无关兼容内容，保持 parent 生命周期。 |
| `src/voidx/presentation/output/tree.py` | todo node 在 `render(width)` 时按真实 branch prefix 从完整 payload 重建宽度投影。 |
| `tui/voidx_cli/layout.py`（新建） | 定义 `RenderedRows`、`RegionGeometry`、`BottomGeometry`、`BottomViewportPlan`、`LogicalRenderPlan`、`PhysicalViewportPlan`、`LayoutSnapshot`、`LayoutDiff` 和安全判定纯函数。 |
| `tui/voidx_cli/state.py` | 保存 applied/pending snapshot、pending terminal operation、generation、submitted generation、scroll epoch、full invalidation 和 terminal failure 状态。 |
| `tui/voidx_cli/render_todo.py` | 消费共享 todo plan，配置 global 4-row budget、pinned prefix/style 和有效宽度。 |
| `tui/voidx_cli/render_frame.py` | 生成逻辑 plan、物理 viewport、bottom 子几何和完整 target；执行 sync patch adapter；提交 worker frame；统一 cursor。 |
| `tui/voidx_cli/render_activity.py` | 只提供 vibe 内容，不自行持有终端几何。 |
| `tui/voidx_cli/app.py` | 输入、timer、scheduled render、commit/barrier 和 frame result 接入统一入口/失效状态机。 |
| `tui/voidx_cli/terminal_writer.py` | 保持公共 API，修改 worker 内部精确 diff、旧尾逐行 EL 和 baseline/result 语义。 |
| `tui/tests/test_todo_rendering.py`（新建） | 覆盖共享 todo 算法与 global/subagent policy 差异。 |
| `tui/tests/test_output_tree.py` | 覆盖 subagent todo 的 branch-prefix-aware 裁剪和 resize 后重投影。 |
| `tui/tests/test_layout.py`（新建） | 覆盖 RenderedRows、geometry、bottom separator、viewport 投影、diff 和物理安全纯函数。 |
| `tui/tests/test_layout_terminal_model.py`（新建） | 用最小 VT screen model 验证 patch 后屏幕、旧尾清除、显式 scroll barrier 和有界 viewport。 |
| `tui/tests/test_frame_rendering.py` | 覆盖单区/suffix 刷新、cursor 与完整物理 viewport 重组。 |
| `tui/tests/test_frame_advanced.py` | 覆盖 resize/restore/commit/scroll、pending/applied、submit/barrier/commit 失败回滚和未知 baseline。 |
| `tui/tests/test_terminal_writer.py` | 覆盖真实 worker started、coalescing、barrier/commit、精确 diff、尾行 EL 和 synchronized output。 |
| `tui/tests/test_terminal_input.py` | 覆盖 pinned todo、输入折行、choice/panel 和 bottom-only 更新。 |
| `tui/tests/test_status_layout.py` | 覆盖 todo/vibe/bottom 顺序及 separator 插入删除。 |
| `tui/tests/test_status_activity.py` | 覆盖多行权限详情、timer 和 thinking 上下游位置。 |
| `src/tests/test_presentation/gateway/test_ui_events_todo.py` | 覆盖 todo 完整 payload、未知状态、subagent 生命周期和完成 snapshot。 |

### 8.2 TDD 顺序

每项必须先写失败测试并确认 RED，再写最小实现并确认 GREEN：

1. `TodoRenderPlan`：稳定排序、未知状态 fallback、global 4-row budget、subagent 8-items-plus-ellipsis、完整 payload；`OutputTree.render(width)` 在不同 branch prefix/resize 宽度下重建 subagent 投影。
2. `RenderedRows`：空区域、尾部空行、SGR/OSC 8、危险控制序列、CJK/emoji、Rich 折行和恰好满宽。
3. 顶层区域与强制 bottom 父/子几何；验证 separator 出现/消失的最早变化位置、非连续 bottom source slice 和 cursor 映射。
4. `LogicalRenderPlan`/`PhysicalViewportPlan`：完整逻辑 source 不受 terminal height 截断；物理 target 始终 `<= terminal_height`；input cursor 行强制可见；viewport 省略行不修改 tree/payload。
5. `LayoutDiff`：同高单区、多个同高区域、高度变化 suffix、旧尾行集合和 cursor-only。
6. 物理安全：frame/cursor/clear rows 越界或 snapshot 不满足 §3.5 不变量时回退完整物理 viewport；需要腾出空间时只滚走已提交可见行。
7. sync adapter：单个 synchronized payload、逐行 `EL`、普通 patch 不含 `CSI J`，flush 后才发布 snapshot；partial write/flush error 发送 `CAN + END_SYNCHRONIZED_OUTPUT + EXIT` 并失效 baseline。
8. worker 内部 diff：移除 80% full 阈值、旧尾逐行 `EL`、完整 synchronized output；callback 尚未执行时仍可基于 worker 私有 baseline 精确 diff。
9. applied/pending 状态机：真实 applied、stale、coalesced、commit/barrier 丢弃、in-flight 旧 callback、`submit_frame()`/`submit_barrier()`/`submit_commit()` 入队失败、token 失败、writer error 和 `full_layout_invalidated` 清除时机。
10. timer、输入、choice、panel、status 和 cursor 全部接入统一入口；移除独立 fast-path 几何判断。
11. 最小 VT screen model：应用 emitted ANSI 后，新屏幕与目标物理 snapshot 一致；超高逻辑内容不把省略行写入 scrollback。
12. commit 单次写入：viewport 省略的 settled tree 行仍由 `_flush_committed()` 完整提交一次，且后续动态 frame 刷新不重复追加同一 transcript。

## 9. 验证命令

### 9.1 基线

```bash
./test.py --backend -- \
  src/tests/test_presentation/gateway/test_ui_events_todo.py \
  tui/tests/test_output_tree.py \
  tui/tests/test_frame_rendering.py \
  tui/tests/test_frame_advanced.py \
  tui/tests/test_terminal_input.py \
  tui/tests/test_status_layout.py \
  tui/tests/test_status_activity.py \
  tui/tests/test_terminal_writer.py \
  -q
```

### 9.2 开发期 focused tests

```bash
./test.py --backend -- \
  src/tests/test_presentation/gateway/test_ui_events_todo.py \
  tui/tests/test_todo_rendering.py \
  tui/tests/test_output_tree.py \
  tui/tests/test_layout.py \
  tui/tests/test_layout_terminal_model.py \
  tui/tests/test_frame_rendering.py \
  tui/tests/test_frame_advanced.py \
  tui/tests/test_terminal_input.py \
  tui/tests/test_status_layout.py \
  tui/tests/test_status_activity.py \
  tui/tests/test_terminal_writer.py \
  -q
```

预期：每个新增行为测试在对应实现前因目标断言失败；完成后全部通过。不能只在未启动 worker 的 fake stdout 模式验证局部刷新。

### 9.3 收尾验证

```bash
./test.py --backend
./test.py --backend -- tui/tests/test_terminal_writer.py -q
git diff --check
```

若全量 backend 受工作树中无关既有修改影响失败，必须列出具体失败、最小复现和因果判断，不能把失败运行报告为通过。

## 10. 不变量与验收标准

### 10.1 Todo

- 任意更新后，完整 item 集合仍可从 state/payload/tree 读取。
- global/subagent 使用同一排序、遗漏计数、ellipsis 生成、cell-width 裁剪和 `RenderedRows` 算法。
- global 保持 4-row body budget 且 ellipsis 占 budget；subagent 保持最多 8 item 且 ellipsis 额外占一行。
- global 只位于 transcript 后的 pinned slot；subagent 只位于对应 parent 下。
- terminal height 不修改 todo policy 或 payload。
- todo 清除后没有旧行残留，也没有 pinned 与完成 snapshot 的重复显示。

### 10.2 布局与行模型

- 顶层 projected 区域行连接后等于完整物理 frame target；所有视觉行数来自同一 `RenderedRows`。
- `"line\n"` 的业务尾空行不会丢失；空区域为 0 行。
- bottom 强制拥有所有 separator；panel 出现/消失时 suffix 从正确 separator 开始，非连续 source slice 仍映射为连续 projected rows。
- cursor 来自同一 bottom snapshot，不依赖分散 `_last_*` 计数；source cursor row 命中且只命中一个逐行 `SourceSlice`。
- source/projected slice 使用 0-based 左闭右开索引；物理 `start_row`/cursor 使用绝对 1-based 坐标。

### 10.3 生产刷新

- 正常 TTY 启动 worker 后，同高 todo/vibe/input/status 更新仍产生精确行 diff，而非无条件 full frame。
- worker 在 `FrameResult` callback 尚未执行时，仍可基于私有 applied baseline 精确 diff。
- baseline 可信时，即使超过 80% 行变化也不使用 `CSI J`。
- suffix 缩短时每个旧尾行分别收到 `CSI K`。
- sync 和 worker 普通 patch 都由 synchronized output 包裹。
- worker 只有在 `FrameResult(applied=True)` 后提升 renderer snapshot；coalesced、invalidated、stale 或旧 epoch frame 不成为 baseline。
- `submit_frame()`、`submit_barrier()` 或 `submit_commit()` 入队抛错时删除对应 pending operation，不应用业务状态，generation 永不复用，并进入统一 writer failure/cleanup。
- barrier/commit token 失败时不应用 commit state、不恢复旧 baseline，且与 writer error 使用同一个幂等失效入口。
- `full_layout_invalidated` 只有当前 epoch 的完整 frame 真正 applied（或同步完整 payload 成功 flush）后清除；普通 callback、`applied=False`、coalescing 和 token 尚未完成不能清除。
- commit/barrier/writer error 后，下一 frame 必须 full，不能基于已失效几何局部写入。

### 10.4 物理边界

- 多行 permission detail、loop waiting/turn active、空/非空 panel、输入折行和 todo 高度变化均更新正确绝对行。
- 任何 patch row、clear row 或 cursor 越出 `1..terminal_height` 时都不执行局部 patch。
- 超高逻辑内容保留完整 `LogicalRenderPlan`；物理 `target_lines` 始终 `<= terminal_height`，且 cursor 位于 projected bottom。
- viewport 省略的 settled 行只由现有 commit lane 完整提交一次；后续动态 frame 刷新不把同一 transcript 重复写入 scrollback。
- 普通 patch ANSI 不含 `\x1b[J`；显式 clear、commit、restore 和不可信 baseline 的 full frame 不受此限制。
- bottom 的 input/panel/status 非连续 source selection 不会产生越界 cursor；每个 cursor source row 都能通过唯一逐行 slice 计算绝对 cursor。
- frame/barrier/commit 入队或 token 失败后不会应用业务状态，writer 进入失败态且后续提交停止；失败入口重复调用不会重复 cleanup。

## 11. 后续阶段衔接

本阶段产出的纯函数链：

```text
LogicalRenderPlan → PhysicalViewportPlan → LayoutSnapshot → LayoutDiff
```

是后续 terminal scheduler 的稳定输入。当前 sync/worker adapter 只负责提交；后续 scheduler 应替换 adapter 和跨来源排序，不重写 todo、行解析、几何或 diff。

后续单独设计并审批：

- `AssistantStreamCommitted` projection task 与 `TurnCompleted` 的 happens-before；
- `UiEventBus.drain()` 等待 pending projection；
- `emit_direct()`、同步 `commit_stream()`、工具 settle、fallback 和下一轮 start 的串行化；
- dynamic tree 连续完成前缀扫描、提交后摘除和释放；
- 移除历史数量/字节淘汰，以及 transcript 的应用层 height/tail 截断；
- writer 接受 commit payload 后失败的恢复与统一 restore 策略。

本阶段处理 frame generation 的 applied/pending baseline，只为保证布局 diff 正确；它不承担不同 UI 生产源之间的全局 happens-before，也不替代后续 UI commit lane。
