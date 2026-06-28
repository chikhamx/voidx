> **Status: Done**

# 行号漂移回退匹配 — 技术设计文档

## Context

### 问题

voidx 的编辑工具依赖 LLM 上下文中 `read` 输出的行号来定位编辑位置。其中 `replace` 还依赖 anchor 在行号附近匹配；一旦 `read` 输出进入上下文就**不可变**，后续编辑产生的行号漂移不会反映到上下文里已有的 `read` 输出中。

现有机制：
- `remap_read_coverage_from_file_diff`（`src/voidx/tools/file_state.py:107`）在编辑后重映射 `ctx.file_read_coverage` 里的已读范围，但这是 **runtime 内部状态**，LLM 看不到。
- `_line_shift_hints`（`src/voidx/tools/file_ops/edit_execute.py:269`）返回 `Line shift: lines after N shifted by ±M` 提示，但 LLM 需要自己心算累积偏移，多次编辑后极易出错。
- `_find_text_segment`（`src/voidx/tools/file_ops/edit_resolve.py:40`）的 anchor 搜索半径固定为 `TEXT_REPLACE_LINE_RADIUS = 3`（`src/voidx/tools/file_ops/types.py:11`），一次中等编辑（如 11 行→5 行，偏移 -6）就能让 anchor 搜索超出半径而失败。

### 典型失败场景

```
read 1-100          → 上下文记录: 25\tdef foo():
edit 20-30 → 5行    → 偏移 -6，第 25 行实际变成第 19 行
edit 老行号 25-25   → anchor 在 ±3 (22-28) 搜索 → 找不到 "def foo():" → 报错
```

LLM 被迫重新 `read`，浪费 token 和轮次。

### 为什么现在做

runtime 已经维护了行号映射信息（`DiffSpan` 列表 + coverage ranges），但这些信息只用于 coverage 检查，**没有反馈到 `replace` 的 anchor 搜索路径**。本设计把每次 `read` 视为一个 read epoch，并维护“该 read 输出中的行号 → 当前文件行号”的映射；当 anchor 搜索失败时，runtime 可以用这些映射重试，而不是让 LLM 心算偏移。

## Goals and Non-Goals

### Goals

- edit 工具在 anchor 搜索失败时，能利用 runtime 已知的 read epoch → 当前文件行号映射重试匹配
- 复用现有 `file_read_coverage` 生命周期，在其中保存 read epoch 的漂移映射，不引入脱离 coverage 的并行状态
- 保持 `_find_text_segment` 的纯函数特性，漂移换算作为外层回退逻辑
- 仅覆盖 `replace` 路径（`_find_text_segment` 的 anchor 搜索失败场景）

### Non-Goals

- 不修改 `read` 工具的输出格式（不向上下文注入行号映射表）
- 不改变 `TEXT_REPLACE_LINE_RADIUS` 的默认值（回退路径独立于半径）
- 不处理外部进程修改文件的情况（已有 `check_staleness` 拦截）
- 不处理跨文件编辑的行号漂移
- 不覆盖 `write`（insert）路径：insert 走 `_apply_resolved_edits`（`edit_execute.py:198`），直接用 `edit.start_line` 做切片，**不调用 `_find_text_segment`**，没有 anchor 搜索失败这一模式。insert 的行号漂移是另一个问题（行号越界换算），不在本设计范围内

## Architecture

### 现有数据流

```
read 1-100
  └─ record_read_range → ctx.file_read_coverage[key] = {fingerprint, ranges:[{1,100}]}

edit 20-30 → 5行
  ├─ _find_text_segment(lines, 20, 30, ...) → 匹配成功
  ├─ check_read_coverage(ctx, path, 20, 30) → 通过
  ├─ path.write_text(...)
  └─ remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges)
       └─ 用 DiffSpan 列表重映射 ranges → [{1,14}, {15,94}]
          (但 LLM 上下文里的 read 输出仍是旧行号)

edit 老行号 40-50  ← LLM 基于上下文里的旧行号
  └─ _find_text_segment(lines, 40, 50, ...) → anchor 在 ±3 找不到 → 报错 ❌
```

### 目标数据流

```
edit 老行号 40-50
  └─ _find_text_segment(lines, 40, 50, ...) → 失败
     └─ 回退: 查 file_read_coverage 的 read epoch drift maps
        ├─ epoch #7: 老行号 40 → 当前行号 34
        ├─ epoch #7: 老行号 50 → 当前行号 44
        └─ _find_text_segment(lines, 34, 44, ...) → 重试匹配 ✅
```

### 模块边界

回退逻辑放在 **`edit_resolve.py`**（纯匹配层）和 **`edit_execute.py`**（调用层）之间：

- `edit_resolve.py`：新增 `remap_line_range` 纯函数，接收 `span_steps: list[list[DiffSpan]]` 和 read epoch 中的老行号范围，返回当前文件行号范围。不依赖 `ctx`。
- `file_state.py`：新增 `get_line_drift_maps(ctx, resolved) -> list[LineDriftMap]`，从 `file_read_coverage` 中提取候选 read epoch 映射。
- `edit_execute.py`：在 `_find_text_segment` 返回字符串（失败）时，遍历候选 drift maps：换算行号 → 重试匹配 → 要求唯一成功。

## Data Model

### 现有结构（不修改）

```
DiffSpan                          # src/voidx/tools/file_state.py:30
├── old_start: int                # 旧文件中的起始行号
├── old_end: int                  # 旧文件中的结束行号
└── offset: int                   # new_count - old_count

file_read_coverage[key]           # src/voidx/tools/base.py:119
├── fingerprint: dict             # {mtime_ns, size}
└── ranges: list[dict]            # [{start_line, end_line}, ...]  ← 已重映射的新行号
```

### file_read_coverage 扩展

在 `file_read_coverage[key]` 中新增可选字段 `line_drift_maps`，存储多个 read epoch 到当前文件的映射。每次 `read` 追加一个新的 epoch map；每次 edit 把本次 `file_diff` 产生的 `DiffSpan` 作为一个新 step 追加到所有现存 epoch map。这样每个 map 的语义始终是：

> 从该 read epoch 输出里的行号，按 `span_steps` 顺序投影到当前文件行号。

这避免了把不同坐标系的 `DiffSpan` 强行拍平成一个列表。每个 step 的坐标系都是"上一个 step 应用后的文件"，顺序应用天然正确。

```
file_read_coverage[key]
├── fingerprint: dict
├── ranges: list[dict]            # 已有：重映射后的已读范围（新行号）
└── line_drift_maps: list[dict]   # 新增：read epoch → 当前文件的候选映射
    └── [
          {
            "epoch": 7,
            "source_ranges": [{start_line, end_line}],
            "span_steps": [
              [{old_start, old_end, offset}],   # edit 1: epoch #7 坐标 → edit 1 后坐标
              [{old_start, old_end, offset}],   # edit 2: edit 1 后坐标 → edit 2 后坐标
            ],
          },
        ]
```

#### 必须修改的现有写入路径

当前 `record_read_range`（`file_state.py:100`）和 `remap_read_coverage_from_file_diff`（`file_state.py:147`）都是**覆盖式写入**——重建整个 dict 只保留 `fingerprint` 和 `ranges`，不保留任何额外字段。要承载 `line_drift_maps`，这两个函数必须改为**保留并更新**既有字段：

- **`record_read_range`**（read 时调用）：当前在 fingerprint 不匹配时丢弃 `ranges`（`file_state.py:99`），且第 100 行整体重建 dict。需改为：读取既有 `line_drift_maps`（若 fingerprint 匹配则保留，不匹配则视为新文件清空），追加一个新的 map（`span_steps=[]`，`source_ranges` 为本次 read 范围），再写回。已有 maps 不清空，因为 LLM 上下文里可能同时存在同一文件的旧 read 输出和新 read 输出。
- **`remap_read_coverage_from_file_diff`**（edit 时调用）：当前第 147 行重建 dict 只写 `fingerprint` + `ranges`。需改为：读取既有 `line_drift_maps`，把本次 `file_diff` 的 spans append 到每个 map 的 `span_steps`，再连同重映射后的 `ranges` 一起写回。若 `ranges` 为空走 `clear_read_coverage` 分支（`file_state.py:151-152`），整个 key 被删除，`line_drift_maps` 一并丢失——符合预期（见 Error Handling）。
- 为避免 unbounded growth，保留最近 `MAX_LINE_DRIFT_MAPS_PER_FILE` 个 maps（建议 8 或 16），超出上限时丢弃 epoch 最小的 map（FIFO）。若未来能从消息裁剪/compaction 得知哪些 read 输出仍在上下文，可改为按真实可见 epoch 回收。

### `LineDriftMap` 运行时模型

实现中可用轻量 dataclass 表示从 dict 解析出的 map：

```
LineDriftMap
├── epoch: int
├── source_ranges: list[ReadLineRange]
└── span_steps: list[list[DiffSpan]]
```

`epoch` 为 **per-file 单调递增整数**，生成规则：`record_read_range` 追加新 map 时取 `max(现有 epochs, default=0) + 1`。FIFO 淘汰后旧 epoch 不复用，保证 epoch 与 read 输出的对应关系在 maps 生命周期内稳定。`epoch` 只用于调试、提示和候选排序，不要求 LLM 在工具参数里传入。

## API Contract

### `remap_line_range`（纯函数，edit_resolve.py）

- **Signature**: `remap_line_range(start: int, end: int, span_steps: list[list[DiffSpan]]) -> tuple[int, int] | None`
- **Input**: read epoch 中的老行号范围 + 该 epoch 到当前文件的 step 序列
- **Output**: `(current_start, current_end)`；若该范围已被后续 edit 完全删除/替换，或被拆分成多个不连续区间，返回 `None`
- **Logic**:
  1. 初始 pending ranges 为 `[{start, end}]`
  2. 对 `span_steps` 顺序处理：每个 pending range 调用 `_remap_old_range(range.start, range.end, step_spans)`
  3. 下一轮 pending ranges 使用所有 remapped ranges
  4. 若任一步后 ranges 为空，返回 `None`
  5. 若最终 ranges 数量 **不等于 1**，返回 `None`（见下方"部分删除"说明）
  6. 取唯一 range 的 `start_line` 作为 `current_start`，`end_line` 作为 `current_end`
- **依据**：直接复用 `_remap_old_range`，保证 fallback 的行号投影与 coverage 重映射同源。被替换/删除的旧内容没有稳定行号；这类 map 应跳过，而不是猜测替换区域前后的位置。
- **部分删除处理**：`_remap_old_range`（`file_state.py:156`）在 range 跨越 span 边界时会切分出**多个不连续区间**。例如 read epoch 的 `[40, 50]` 被中间一次 edit 切成 `[34, 36]` 和 `[40, 44]` 两段。此时取首末拼接会得到 `(34, 44)`——一个**跨越被删除行**的虚假连续区间，`_find_text_segment` 在其中搜索 anchor 会命中错误上下文。因此 `remap_line_range` 要求最终结果为**单一连续区间**，多段时返回 `None` 跳过该 map。这比 Decision #6 的"完全删除返回 None"更强：部分落入删除区同样跳过。

### `get_line_drift_maps`（file_state.py）

- **Signature**: `get_line_drift_maps(ctx: ToolContext, resolved: Path) -> list[LineDriftMap]`
- **Output**: `ctx.file_read_coverage[key]["line_drift_maps"]` 解析后的 maps，若不存在则返回 `[]`
- **Errors**: 无。文件未追踪时返回空列表。
- **指纹检查说明**：`line_drift_maps` 与 `fingerprint` 在 `record_read_range` / `remap_read_coverage_from_file_diff` 中同一事务写入；`check_staleness` 已在 edit 入口（`edit_execute.py:103`）拦截过期文件。因此本函数不做独立指纹校验，真正防线仍是 staleness。

### `_find_text_segment_with_drift_fallback`（edit_execute.py，内部）

- **Signature**: 
  ```python
  def _find_text_segment_with_drift_fallback(
      lines: list[str],
      start_no: int,
      end_no: int,
      prefix: str,
      suffix: str,
      maps: list[LineDriftMap],
  ) -> DriftFallbackResult
  ```
  **调用点**：替换 `edit_execute.py:128`（`_execute_text_replace` 中）对 `_find_text_segment` 的直接调用。原调用 `match = _find_text_segment(display.lines, start_no, end_no, start_anchor, end_anchor)` 改为先通过 `get_line_drift_maps(ctx, path)` 取候选 maps，再调用本函数。返回 `DriftFallbackResult` 后，调用方根据 `match`/`error` 分支处理，与原 `isinstance(match, str)` 判断等价。
  其中 `DriftFallbackResult` 为 NamedTuple
  ```python
  class DriftFallbackResult(NamedTuple):
      match: tuple[int, int, int, int] | None   # _find_text_segment 的成功结果
      error: str | None                          # 失败时的错误信息
      matched_map: LineDriftMap | None           # 回退命中时的 epoch map
      remapped_range: tuple[int, int] | None     # 回退命中时换算后的 (start, end)
  ```
  `matched_map` 与 `remapped_range` 要么同时为 None（首次匹配成功或全部失败），要么同时非 None（回退命中），语义上无冗余歧义。
- **Logic**:
  1. 调用 `_find_text_segment(lines, start_no, end_no, prefix, suffix)`
  2. 若成功，返回 `DriftFallbackResult(match=match, error=None, matched_map=None, remapped_range=None)`
  3. 若失败（返回 str）且 `maps` 非空：
     - 按 `epoch` 从新到旧遍历 maps
     - `remapped = remap_line_range(start_no, end_no, map.span_steps)`
     - 若 `remapped is None` 或与原始行号相同，跳过
     - 调用 `_find_text_segment(lines, remapped_start, remapped_end, prefix, suffix)`
     - 收集所有成功候选
  4. 若没有候选成功，返回 `DriftFallbackResult(match=None, error=第一次错误, ...)`
  5. 若恰好一个候选成功，返回 `DriftFallbackResult(match=match, matched_map=map, remapped_range=remapped)`
  6. 若多个候选成功但 resolved `(start_line, end_line)` 相同，返回该结果（等价命中）
  7. 若多个候选成功且 resolved range 不同，返回 ambiguity 错误，要求重新 read
- **失败分类（不显式区分，但语义上）**：第一次失败可能源于 (a) 行号漂移——anchor 内容在文件中存在，只是位置偏移；(b) anchor 内容错误——anchor 在文件中根本不存在。回退对 (a) 有效；对 (b) 换算行号后仍找不到 anchor，必然失败并返回原错误。回退路径不区分这两种情况（区分需要全文搜索 anchor，成本高且可能误匹配），而是统一"换算→重试"，让 (b) 自然失败。这是可接受的：回退的额外开销仅一次 `_find_text_segment` 调用（O(radius)），不会显著拖慢错误路径。
- **回退成功后的 coverage 检查衔接**：回退命中后，`_execute_text_replace` 后续的 `check_read_coverage`（edit_execute.py:133）与切片（:147 `lines[start_line-1:end_line]`）使用回退换算后的行号（当前文件坐标系），无需二次换算。`old_ranges` 提取逻辑（:140-143）不变——它从 `existing_coverage["ranges"]` 整体拷贝用于后续 `remap_read_coverage`，与本次编辑范围无关；这些 ranges 已被 `remap_read_coverage` 重映射为当前文件行号，与回退后的行号坐标系一致。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `line_drift_maps` 不存在（文件首次 read 后未编辑过） | `get_line_drift_maps` 返回 `[]`，回退路径不触发，行为与现状一致 |
| 漂移换算后 range 被完全删除/替换 | `remap_line_range` 返回 `None`，跳过该 map |
| 漂移换算后 range 被拆分成多个不连续区间 | `remap_line_range` 返回 `None`，跳过该 map（见 API Contract 的"部分删除处理"） |
| 换算后仍匹配失败 | 返回**第一次**的错误信息（基于老行号），错误信息中附加上下文窗口仍基于老行号 |
| 多个 epoch map 都匹配成功且目标范围不同 | 返回 ambiguity 错误，要求重新 read，避免误改 |
| 换算后匹配成功但 coverage 检查失败 | 正常返回 coverage 错误，不特殊处理。坐标系链路：LLM 用某个 read epoch 的旧行号 → 回退换算 → 当前文件行号；而 coverage 的 ranges 也已被 `remap_read_coverage` 重映射为当前文件行号，两者一致时应能通过 |
| `old_ranges` 全部落入删除区域 → coverage 被清空 | `remap_read_coverage_from_file_diff` 在 `ranges` 为空时走 `clear_read_coverage`（`file_state.py:151-152`），整个 key 被删除，`line_drift_maps` 一并丢失。符合预期：anchor 内容已不存在，回退本就该失败 |
| Compaction 触发 | `compaction.py:68` 和 `:85` 直接 `clear()` 整个 `file_read_coverage`，drift maps 一并丢失。这是**整体清空**而非"需要回收"——compaction 后 LLM 上下文里的旧 read 输出也被摘要掉，后续 edit 会因 `check_staleness`（`file_mtimes` 同步清空）失败要求重新 read，符合预期。drift maps 不跨 compaction 存活，实现不应依赖此假设 |
| anchor 内容错误（非行号漂移） | 回退换算后仍找不到 anchor，返回第一次错误。回退不区分此情况，见 API Contract 的"失败分类" |

### 等价性不变式

`remap_line_range` 每一步必须复用 `remap_read_coverage_from_file_diff` 中 `_remap_old_range`（`file_state.py:156`）的语义。对任意 read epoch 的 range，把该 range 依次通过 `span_steps` 投影后的结果，必须与 coverage ranges 在同一系列 edits 后的坐标系一致。实现时需用测试覆盖此不变式，否则会出现"回退匹配成功但 coverage 失败"的不一致。

### 回退成功的反馈

当回退路径匹配成功时，在 `ToolResult.output` 中追加提示：

```
[Line drift fallback: {file_path} epoch #{epoch} start_no {old_start}→{new_start}, end_no {old_end}→{new_end} matched via drift map.]
```

这让 LLM 知道行号发生了漂移，后续编辑应使用新行号。

## read/edit 时的漂移表更新

### read：追加 epoch，不清空旧 maps

`record_read_range`（read 时调用）追加一个新的 `LineDriftMap`：

```
read 1-100          → maps: [{epoch: 1, source_ranges:[1-100], span_steps: []}]
edit 20-30 → 5行    → maps: [{epoch: 1, span_steps: [[{20,30,-6}]]}]
read 20-30          → maps: [
                       {epoch: 1, span_steps: [[{20,30,-6}]]},
                       {epoch: 2, source_ranges:[20-30], span_steps: []},
                     ]
```

旧 maps 必须保留，因为 LLM 上下文里可能同时存在 epoch #1 和 epoch #2 的 read 输出。工具调用没有 epoch 参数，只能通过 anchors 和候选 map 重试来判断调用方实际引用的是哪份 read 输出。

### edit：给每个 epoch append 一个 step

`remap_read_coverage_from_file_diff` 每次 edit 后构造本次 `file_diff` 的 spans，并 append 到每个 map 的 `span_steps`：

```
read 1-100          → epoch #1: []
edit 20-30 → 5行    → epoch #1: [[{20,30,-6}]]
edit 40-50 → 3行    → epoch #1: [[{20,30,-6}], [{34,44,-8}]]
```

第二个 step 的 `{34,44,-8}` 位于“第一次 edit 后”的坐标系。fallback 计算 epoch #1 的老行号 60 时，先应用第一个 step 得到 54，再应用第二个 step 得到 46。这样不需要把 spans 反向换算回最初 read 坐标系，也不会混用坐标系。

### 多次 edit 不 read 的情况

```
read 1-100          → epoch #1: []
edit 20-30 → 5行    → epoch #1: [[{20,30,-6}]]
edit 40-50 → 3行    → epoch #1: [[{20,30,-6}], [{34,44,-8}]]
edit 老行号 60      → remap: 60 → 54 → 46
```

这是本方案相对“覆盖写入本次 spans”的核心改进：多次 edit 后，旧 read 输出仍可通过 step 序列投影到当前文件。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| #1 漂移信息存在 `file_read_coverage` 里，不新建独立状态 | 新建 `ctx.file_drift_maps` 字典 | `file_read_coverage` 已经在 edit 时更新、read 时记录、staleness 时检查。复用它的生命周期管理，避免两套状态同步问题 |
| #2 `line_drift_maps` 按 read epoch 存储，且每个 epoch 保存 `span_steps` | 单个 `drift_spans` 覆盖写入 / 把所有 spans 反向换算成一个扁平列表 | 覆盖写入只能处理 read 后第一次 edit，无法处理多次 edit 不 read。扁平列表需要把每次 edit 的 spans 反向换算回 read epoch 坐标系，复杂且容易出错。step 序列保留每次 diff 的原生坐标系，顺序应用即可 |
| #3 回退路径返回第一次的错误信息 | 返回第二次（换算后）的错误 | 第一次错误基于 LLM 提供的原始行号，上下文窗口（`_window_snippet`）也基于老行号，对 LLM 更可理解。第二次失败说明换算后仍找不到，返回老错误让 LLM 决定是否重新 read |
| #4 `line_drift_maps` 保留多个 read epoch，fallback 遍历候选并要求唯一命中 | read 时清空旧 drift 信息 / 让 LLM 传 epoch id | read 输出在上下文中不可变且可能并存，read 时清空会让旧输出失去 fallback 能力。工具参数不包含 epoch id，要求 LLM 传 epoch 会改变 read 输出和工具契约 |
| #5 回退成功时追加提示到 output | 静默成功 | LLM 需要知道行号漂移了，否则后续编辑继续用老行号，每次都要走回退。提示让 LLM 自我修正 |
| #6 被替换/删除区域返回 `None`，而不是猜测一个行号 | 映射到替换区域起点 / 映射到替换区域之后第一行 | 被替换的旧内容没有稳定的新行号。跳过该 map 能保持与 `_remap_old_range` 和 coverage 语义一致，避免 fallback 错配。**强化**：部分落入删除区（range 被拆分成多段）同样返回 `None`，因为 anchor 搜索需要连续区间，拼接首末会跨越被删除行产生虚假区间（见 API Contract 的"部分删除处理"） |
| #7 `get_line_drift_maps` 不做独立指纹校验 | 加冗余指纹检查作为防御 | drift maps 与 `fingerprint` 同事务写入，`check_staleness` 已在 edit 入口拦截。冗余检查无害但不应作为关键防线 |

## Open Questions

- [ ] `MAX_LINE_DRIFT_MAPS_PER_FILE` 取 8 还是 16？建议先用 16，避免长任务里旧 read 输出过早失效。
- [ ] ~~是否需要在 message trimming / compaction 后回收不可见 read epoch？~~ **已澄清**：compaction 直接 `clear()` 整个 `file_read_coverage`（`compaction.py:68,85`），drift maps 不跨 compaction 存活，无需也无法回收。当前用 `MAX_LINE_DRIFT_MAPS_PER_FILE` 固定上限 + FIFO 淘汰即可。
- [ ] 是否需要在 `_line_shift_hints` 的输出中也引用 matched epoch？目前 fallback 成功提示已经足够，暂不合并。
