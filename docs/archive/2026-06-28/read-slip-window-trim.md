> **Status: Done**

# Read 滑动窗口惰性裁剪 — 技术设计文档

## Context

当前 read 工具的 ToolMessage 一旦生成，会永久留在对话历史中，直到 compaction 整体清理。同一文件被反复 read 时，旧的 read ToolMessage 内容冗余，浪费上下文 token；且 edit 后旧行号已失效，保留旧 read 可能误导模型。

现有 `file_read_coverage` 机制只维护"行区间集合"用于 edit 前的 staleness 校验，不裁剪历史 ToolMessage。本设计在 `compile_messages` 阶段引入滑动窗口惰性裁剪，针对同一文件的重复 read 做删除，对 edit diff 做摘要化处理。

## Goals and Non-Goals

### Goals

- 窗口内同一文件出现新 read 时，删除被后续仍保留 read 覆盖到阈值的更早 read tool call 及其 ToolMessage（规则 1）
- `replace` / `write`（insert/append）的 ToolMessage 中，unified diff 内容替换为语义摘要（规则 2）
- read 缓存命中（`covered_read_range`）时，不再向输出内容添加 `[Lines X-Y were already read...]` 提示（规则 3）
- 裁剪在 `compile_messages` 阶段惰性执行，不修改 state 原始 messages

### Non-Goals

- 不处理窗口外的 messages（交给 compaction）
- 不处理 subagent 的 messages（第一版）
- 不处理跨文件的裁剪
- 不修改 `file_read_coverage` 的行区间追踪机制（它仍用于 edit 前校验）

## Architecture

### 数据流

```
state["messages"]                         ← 原始 messages，不可变
    │
    ▼
ContextCompiler.compile_messages()        ← runtime_context.py:111
    │
    ├─ raw_semantic_messages()            ← 过滤 SystemMessage / overlay
    ├─ _strip_historical_tool_skill_context()
    ├─ sanitize_todo_replay_messages()
    ├─ _trim_superseded_file_tools()      ← 【新增】滑动窗口惰性裁剪
    │       │
    │       ├─ 从末尾向前累计行数，确定窗口边界
    │       ├─ 窗口内建立 tool_call_id → tool_call / ToolMessage 配对索引
    │       ├─ 窗口内正向扫描，构建 file → read/edit 记录
    │       ├─ 规则 1：同文件新 read 出现时，删除被后续仍保留 read 覆盖到阈值的更早 read
    │       └─ 规则 2：replace/write 的 ToolMessage，diff 内容替换为摘要
    │
    ▼
[SystemMessage, ...semantic_messages]     ← 返回给 LLM
```

### 注入点

`ContextCompiler.compile_messages`（`src/voidx/agent/runtime_context.py:111`），在 `sanitize_todo_replay_messages` 之后、返回 result 之前插入 `_trim_superseded_file_tools`。

### 识别 read vs edit 的方式

`ToolMessage` 构造时（`src/voidx/agent/graph/tool_executor/executor.py:229`）只带 `content`、`tool_call_id`、`status`，不含 tool name 或 metadata。因此需要通过配对的 `AIMessage.tool_calls` 反查：

- `AIMessage.tool_calls` 是 `list[dict]`，每项含 `id`、`name`、`args`
- `ToolMessage.tool_call_id` 对应 `tool_calls[i]["id"]`
- 通过 `tool_calls[i]["name"]` 判断是 `read` / `replace` / `write`
- 通过 `tool_calls[i]["args"]["file_path"]` 获取文件路径

实现时先在窗口内建立两个临时索引：

```python
tool_calls_by_id: dict[str, ToolCallRef]
tool_messages_by_id: dict[str, ToolMessageRef]
```

扫描 AIMessage 时只处理能在 `tool_messages_by_id` 中找到成功 ToolMessage 的 tool_call；扫描 ToolMessage 时只处理能在 `tool_calls_by_id` 中找到配对 tool_call 的结果。这样可以避免在窗口边界或并行 tool_call 场景下做脆弱的“向后找下一个 ToolMessage”。

> **原始 provider tool call 形态**：构建裁剪后 AIMessage 时，除了更新 `message.tool_calls`，还必须同步移除 `additional_kwargs["tool_calls"]` 里的同 id 调用；如果 `message.content` 是 list 且含 `{"type": "tool_use", "id": ...}`，也必须同步移除同 id 项。否则请求中可能残留一个没有 ToolMessage 配对的 raw tool call，导致 LLM API 拒绝或模型看到不一致的工具交换。

> **`file` 工具不纳入规则 2**：`file` 的 create/delete/move 走独立路径（`file_ops/file.py`），output 首行是 `File created:` / `File deleted:` / `File moved:` 而非 `File edited:`，且其 diff 语义是"整文件删除/目标覆盖"而非"局部编辑"。规则 2 的摘要化逻辑（保留 `File edited:` 首行、解析 hunk 行号）对 `file` 不适用。`file` 操作的 diff 仍完整保留。

### read 行范围的获取方式

read 工具的 Input schema（`read.py:169-171`）字段为 `file_path`、`offset`、`limit`，**没有 `start_line`/`end_line`**。且 `offset`/`limit` 均可省略（省略时从头读、读到文件末尾或字符上限截断），因此从 args 算出的"请求范围"与"实际返回范围"经常不一致（截断、EOF 提前结束都会让实际范围更小）。

实际返回的行范围在 ToolResult.metadata 里有 `start_line`/`end_line`（`read.py:279-280`），但 `compile_messages` 阶段拿到的 ToolMessage **不带 metadata**（`executor.py:229` 构造时只传 content/tool_call_id/status）。

**因此行范围必须从 ToolMessage.content 的实际输出解析**。read 输出格式为 `{line_number}\t{line}`（`read.py:110`，`f"{line_number}\t{line}"`）：

- `start_line` = content 首行的行号（tab 前的数字）
- `end_line` = content 末行的行号

`already_read` 命中分支（`read.py:236-263`）的 content 同样带行号，解析方式一致。若 content 为空或无法解析行号，跳过该 read（不纳入索引）。

## Data Model

### 窗口内临时索引（compile 时构建，不持久化）

```python
# 扫描窗口内 messages 时构建，compile 完即丢弃

# 文件 → 当前仍保留 read 的行区间并集（合并后的不相交区间列表，可由 records 重建）
file_read_union: dict[str, list[tuple[int, int]]]

# 文件 → 每条 read 的记录
file_read_records: dict[str, list[ReadRecord]]
# ReadRecord: {msg_index, tool_call_id, ranges, deleted}

# 文件 → 每条 edit 的记录
file_edit_records: dict[str, list[EditRecord]]
# EditRecord: {msg_index, tool_call_id, hunk_ranges, summarized}

# 配对索引
tool_calls_by_id: dict[str, ToolCallRef]
tool_messages_by_id: dict[str, ToolMessageRef]

# 覆盖阈值
COVERAGE_THRESHOLD: float = 0.6
```

### 窗口定义

```python
WindowConfig
└── window_lines: int = 2000          # 窗口覆盖的最近行数
```

> 删除决策完全由覆盖率 60% 驱动，不设 `max_reads_per_file` 硬上限。避免"覆盖率没到 60% 但因数量超限被强制删除"的误删。

> **窗口行数是软上限**：从末尾向前累计，一旦达到 `window_lines` 就停止向前扩展，但当前正在累计的"AIMessage + 其所有 ToolMessage"组合会**整体纳入**窗口（即使超出上限）。因此实际窗口可能略大于 `window_lines`，但不会出现 AIMessage 与 ToolMessage 配对断裂。`window_lines` 控制的是"大约多少行"，不是硬上限。

### 裁剪后的 ToolMessage 形态

**规则 1 — read 被删除**：
- 触发条件：窗口内同一文件出现新的 read tool_call 时，该文件在 `file_read_records` 中记录的更早 read 会重新计算覆盖率；只有被**该 read 之后的仍保留 read** 覆盖比例达到 `COVERAGE_THRESHOLD` 的旧 read 才会被删除
- AIMessage 的 `tool_calls` 列表移除对应条目，用 `model_copy(update={"tool_calls": [...]})` 生成新 AIMessage
- 同步移除 `additional_kwargs["tool_calls"]` 和 content-list 中同 id 的 raw `tool_use` 项（若存在）
- 对应的 ToolMessage 从 messages 中移除
- 若 AIMessage 移除后 `tool_calls` 为空：
  - 若该 AIMessage 有文本内容 → 保留 AIMessage（只删 tool_call 部分）
  - 若该 AIMessage 无文本内容 → AIMessage 一并移除
- **多 tool_call 部分移除**：一个 AIMessage 可能带多个 tool_call（并行执行场景，如同一轮发起多个 read）。删除其中某个 read 的 tool_call 时，AIMessage 的 `tool_calls` 列表做部分移除，剩余 tool_call 的 id 与剩余 ToolMessage 仍一一配对，配对关系不变。这是常态场景，部分移除合法。

**规则 2 — edit diff 摘要化**：

> **关键依赖：edit input 的 new_string 在下一轮已被 prune 清空**
>
> 现有机制（`src/voidx/llm/compaction.py:120-162`，`_prune_ai_tool_call_args`）在**每轮结束后**（`turn_runner.py:298`）执行 prune，对上一轮及更早的 AIMessage tool_calls：
> - `replace` 工具的 `new_string` → 替换为 `"[omitted: see diff in tool result]"`
> - `write` 工具的 `content` / `new_string` → 同理
> - **条件**：仅当对应 ToolMessage 里有 diff（`---` 和 `+++`）时才清空，因为"LLM 可以通过 diff 看到内容"
>
> 这意味着：
> - **edit 执行当轮**：input `new_string` 完整，output diff 完整
> - **下一轮起**（prune 后）：input `new_string` = `[omitted: see diff in tool result]`，**output diff 是唯一的内容来源**
>
> **因此规则 2 不能无脑删除 diff 的 `+` 行**，否则模型在下一轮完全丢失"改了什么内容"的信息。

### 规则 2 修正后的作用域

规则 2 只在**窗口内出现同一文件的新 read**时触发，且只对**被新 read 覆盖的 edit diff** 做摘要化。

理由：
- 新 read 已经提供了文件最新内容，edit diff 的 `+` 行（改动后的内容）已被新 read 包含 → diff 冗余，可摘要化
- 如果没有新 read 覆盖，edit diff 必须保留（它是 prune 后唯一的内容来源）
- 不满足覆盖条件（覆盖率 <60%）时，edit diff 完整保留

### 覆盖判定标准（集合维度 + 60% 阈值）

当前文件在窗口内所有**仍保留**的 read 构成一个**已读行区间集合**。每次新 read 到来时，把它的范围记为一条新的 `ReadRecord`，再基于未删除记录重建并集。read 删除判定基于**目标 read 之后的仍保留 read 并集**，而非单条 read 对单条 read，也不能把被检查的 read 自己或它之前的 read 算入覆盖证据。

#### 核心数据结构：已读行区间并集

```python
# 每个文件维护当前仍保留 read 的行区间并集（合并后的不相交区间列表）
file_read_union: dict[str, list[tuple[int, int]]]
# 例: {"src/foo.py": [(1, 100), (150, 200)]}
# 表示该文件在窗口内已读行 1-100 和 150-200

# 覆盖阈值：旧 read 范围被后续仍保留 read 并集覆盖的比例达到此值即删除
COVERAGE_THRESHOLD = 0.6  # 60%
```

每次新 read 到来时：
1. 把新 read 的 `[start, end]` 追加为一条未删除 `ReadRecord`
2. 对每条更早且未删除的 read，使用 `retained_union(file, after_msg_index=old_read.msg_index, exclude_tool_call_id=old_read.tool_call_id)` 计算覆盖率
3. 覆盖率 ≥60% → 标记旧 read 为待删除（规则 1）
4. 每次删除标记变化后，用所有未删除 read 记录重建 `file_read_union[file]`
5. 检查每条 edit diff：计算其 hunk 行范围被该 edit 之后的仍保留 read 覆盖的比例，≥60% → 触发规则 2 摘要化

#### union 维护规则

`file_read_union` 的语义是"**当前仍保留在历史中的 read** 所覆盖的范围"。被删除的 read 已从历史移除，其区间不应再算作"已读"。删除某条 read 时，只能用它之后的 read 作为覆盖证据；被检查的 read 自己和它之前的 read 都不能作为覆盖证据，否则会出现自我覆盖或用旧证据删除新证据的误删。

因此推荐只把 `file_read_union` 当作派生缓存，而不是主状态：

- 主状态是 `file_read_records[file]` 中每条 read 的 `ranges` 和 `deleted` 标记
- `file_read_union[file]` 始终由未删除 records 重建
- 判定某条旧 read 是否可删时，使用该 read 之后、且排除该 read 自身后的 union

#### 覆盖比例计算

```python
def coverage_ratio(target_ranges: list[tuple[int, int]], union: list[tuple[int, int]]) -> float:
    """计算 target_ranges 被 union 覆盖的比例。target_ranges 可为 remap 后的多段。"""
    old_total = sum(end - start + 1 for start, end in target_ranges)
    if old_total <= 0:
        return 0.0
    covered = 0
    for old_start, old_end in target_ranges:
        for s, e in union:
            overlap_start = max(old_start, s)
            overlap_end = min(old_end, e)
            if overlap_start <= overlap_end:
                covered += overlap_end - overlap_start + 1
    return covered / old_total
```

#### 规则 1：旧 read 被并集覆盖 ≥60% 即删除

```
删除条件：coverage_ratio(
    old_read.ranges,
    retained_union(file, after_msg_index=old_read.msg_index, exclude_tool_call_id=old_read.tool_call_id),
) >= 0.6
```

示例：
```
旧 read: 1-100（共 100 行）
新 read: 10-30, 40-70, 71-100
并集: [(10, 30), (40, 100)]
覆盖行数: 21 + 31 + 30 = 82
覆盖率: 82/100 = 82% >= 60% → 删除
```

```
旧 read: 1-100（共 100 行）
新 read: 10-30, 40-70
并集: [(10, 30), (40, 70)]
覆盖行数: 21 + 31 = 52
覆盖率: 52/100 = 52% < 60% → 保留
```

#### 规则 1 的特殊情况：edit 后部分清理（remap + 覆盖率判定）

如果窗口内同一文件的序列是 `read → edit → read`（两次 read 之间有 edit），edit 会导致行号偏移。处理方式：

**不是清空整个并集**，也不是"有交集即删除"，而是：
1. 对 `file_read_union` 中 edit 之前的旧区间，用 edit 的 hunk 偏移做 **remap**（复用现有 `remap_read_coverage_from_file_diff` / `_remap_old_range` 逻辑，`file_state.py:156`），把旧区间映射到 edit 后的新行号
2. remap 后范围为空（整段被删）→ 标记该旧 read 为待删除
3. remap 后范围非空 → 更新该 read record 的 `ranges`，保留整条 ToolMessage
4. 用未删除 read records 重建 `file_read_union[file]`
5. edit 本身不因为覆盖率删除旧 read；覆盖率删除只在后续新 read 到来时触发，且覆盖 union 必须只包含被检查 read 之后的仍保留 read

> **为什么不用"有交集即删除"**：一个 1000 行的大 read，edit 只动了其中 5 行，按"有交集"就得整条删除——但 remap 后 995 行的行号仍有效，删掉损失太大。Decisions Log 里也写了"有交集太激进会删掉仍有价值的部分"，edit 后清理应与规则 1 用同一把尺子。

> **ToolMessage 粒度**：remap 后旧 read 可能被拆成多段（`_remap_old_range` 返回 `list[dict]`）。覆盖率计算要对所有段求并集覆盖比例，不能只看第一段。若后续 read 对这些 remapped 段的整体覆盖率 <60%，保留整条 read（ToolMessage 不可部分保留），union 里用 remap 后的多段区间。

> **DiffSpan 构造方式**：`_remap_old_range` 签名为 `(start, end, spans: list[DiffSpan])`，需要 `DiffSpan(old_start, old_end, offset)`。在 compile 阶段没有 `FileDiff` 对象，需从 ToolMessage 的 unified diff 文本解析：
> - 每个 hunk header `@@ -old_start,old_count +new_start,new_count @@`
> - `old_end = old_start + old_count - 1`（`old_count` 省略时默认为 1）
> - `offset = new_count - old_count`
> - 按 `old_start` 升序排列
>
> 这与 `remap_read_coverage_from_file_diff`（`file_state.py:119-126`）构造 DiffSpan 的逻辑一致，只是数据来源从 `FileDiff.hunks` 换成 diff 文本解析。

具体流程：
```
edit 发生时：
  for old_read in file_read_records[file]:
    remapped = _remap_old_range(old_read.start, old_read.end, edit_spans)
    if remapped 为空:
      → 标记该旧 read 为待删除（整段被删）
    else:
      → 更新 old_read.ranges 为 remapped（行号偏移后仍有效），保留
  用未删除 read records 重建 file_read_union[file]
```

#### 规则 2：edit diff 被并集覆盖 ≥60% 即摘要化

edit diff 的 hunk 行范围被该 edit 之后的仍保留 read 覆盖比例 ≥60% 时触发摘要化。阈值与规则 1 一致，但覆盖证据必须来自 edit 之后的 read，因为 edit 之前的 read 不包含编辑后的 `+` 行内容。

```
触发条件：coverage_ratio(edit.hunk_ranges, retained_union(file, after_msg_index=edit.msg_index)) >= 0.6
```

多 hunk 时，把所有 hunk 的行范围合并为一个集合，计算整体覆盖率。

理由：
- edit diff 的 `+` 行是改动后内容，新 read 覆盖了 ≥60% 的改动行 → diff 大部分冗余
- 与规则 1 用同一阈值，逻辑统一
- 覆盖率 <60% 时 diff 仍有较多未覆盖内容，保留更安全

#### 场景验证

**场景 1：多次小范围 read → 多次重叠 read**
```
旧: read 1-10, read 11-20, read 21-30
新: read 5-15, read 16-26, read 27-37
```
新 read 逐步并入并集：
- read 5-15 → 并集 [(5, 15)]
  - 旧 1-10：覆盖 5-10 = 6/10 = 60% → **删除**
  - 旧 11-20：覆盖 11-15 = 5/10 = 50% < 60% → 保留
  - 旧 21-30：覆盖 0% → 保留
- read 16-26 → 并集 [(5, 26)]
  - 旧 11-20：覆盖 11-20 = 10/10 = 100% → **删除**
  - 旧 21-30：覆盖 21-26 = 6/10 = 60% → **删除**
- read 27-37 → 并集 [(5, 37)]，无剩余旧 read
- 最终：三条旧 read 全部删除

**场景 2：大范围 read → 多次小范围 read**
```
旧: read 1-100
新: read 10-30, 40-70, 71-100
```
- read 10-30 → 并集 [(10, 30)]，覆盖率 21/100 = 21% → 保留
- read 40-70 → 并集 [(10, 30), (40, 70)]，覆盖率 52/100 = 52% → 保留
- read 71-100 → 并集 [(10, 30), (40, 100)]，覆盖率 82/100 = 82% → **删除**
- 最终：旧 read 在第三条新 read 后被删除

**场景 3：大范围 read → edit → 多次小范围 read**
```
旧: read 1-100
edit 20-30（假设替换为 20-25，净减 5 行）
新: read 1-20, read 30-50
```
- edit 发生时：
  - 旧 read 1-100 remap：1-19 不受影响（行号不变），20-30 区域被 edit 覆盖（remap 后该段消失），31-100 remap 后为 26-95（偏移 -5）
  - remap 后旧 read 范围 = [1-19] ∪ [26-95]，共 89 行
  - 此时 union 仍只有旧 read 自己的 remapped 范围；edit 阶段只 remap，不做覆盖率删除，避免自我覆盖
  - 保留旧 read，union 更新为 [(1,19), (26,95)]
- read 1-20 → 新 read 记录加入后，检查旧 read 时排除旧 read 自身，只用新 read 的 union [(1,20)] 计算覆盖率
  - 旧 read remapped 范围 [1-19]∪[26-95] 被新 read 覆盖：1-19 全覆盖(19行) + 26-95 未覆盖(0行) = 19/89 = 21% <60% → 保留旧 read
- read 30-50 → 并入 union → [(1,20), (30,50)]
- 再次检查旧 read 时排除旧 read 自身，只用新 read union [(1,20), (30,50)]：覆盖 1-19(19行) + 30-50(21行) = 40/89 = 45% <60% → 仍保留
- 最终：旧 read 不会因为小范围后续 read 被误删；除非后续 read 对 remapped 范围的覆盖达到 60%

> **对比旧方案**：旧方案用"有交集即删"，edit 发生时就删旧 read。新方案走覆盖率，edit 时只做 remap 不删除，等到新 read 覆盖率达标才删。这样如果 edit 后没有新 read 覆盖，旧 read 仍保留（diff 是唯一内容来源时更安全）。

### 摘要化后的形态

ToolMessage content 从：
```
File edited: src/foo.py (1 operations)
@@ -20,7 +20,12 @@
 context line
-old line
+new line
 context line
```
变为：
```
File edited: src/foo.py (1 operations)
Changed lines: 20-31 (full content available in recent read of this file)
```

注意：摘要化后 `+new line` 被删除，但因为新 read 已覆盖该范围，模型可从 read 的 ToolMessage 中看到最新内容，不会丢失信息。末尾的指向性提示帮助模型定位信息来源，与 prune 的 `[omitted: see diff in tool result]` 风格一致。

## API Contract

### `_trim_superseded_file_tools`

- **Signature**:
  ```python
  def _trim_superseded_file_tools(
      messages: list[BaseMessage],
      *,
      window_lines: int = 2000,
  ) -> list[BaseMessage]
  ```
- **Input**: `raw_semantic_messages` + `sanitize_todo_replay_messages` 处理后的 messages 列表
- **Output**: 裁剪后的 messages 列表（新列表，不修改输入）
- **行为**:
  1. 从末尾向前累计 message 行数，直到达到 `window_lines`，确定窗口起始索引
     - **窗口边界落在 message 边界上**：一个 AIMessage 及其所有 ToolMessage 要么全在窗口内要么全在外，不能按纯行数硬切（避免 AIMessage 与其 ToolMessage 配对断裂）
  2. 在窗口内先建立配对索引：
     - `tool_calls_by_id`：从 AIMessage 的 `tool_calls`、`additional_kwargs["tool_calls"]`、content-list raw `tool_use` 中收集 tool call id。若同 id 同时存在规范化和 raw 形态，以 `message.tool_calls` 的 `name` / `args` 为准。
     - `tool_messages_by_id`：从 ToolMessage 的 `tool_call_id` 收集结果。
     - 只有同时存在 tool call 和成功 ToolMessage 的 id 才进入 read/edit 裁剪逻辑；缺失配对的消息保持原样。
  3. 窗口内正向扫描，维护以下状态（按文件路径）：
     - `file_read_union: dict[str, list[tuple[int, int]]]` — 文件 → 已读行区间并集（合并后的不相交区间）
     - `file_read_records: dict[str, list[ReadRecord]]` — 文件 → 每条 read 的记录（含 msg_index、tool_call_id、行范围），用于判定旧 read 是否被并集覆盖 ≥60%
     - `file_edit_records: dict[str, list[EditRecord]]` — 文件 → 每条 edit 的记录（含 msg_index、tool_call_id、diff hunk 行范围），用于判定 edit diff 是否被并集覆盖 ≥60%
  4. 遇到 AIMessage 时，检查其有效 tool_calls：
     - 对 `name == "read"` 的 tool_call，取 `args["file_path"]` 和行范围 `[start, end]`：
       - 把当前 read 追加到 `file_read_records[file]`
       - 用所有未删除 read records 重建 `file_read_union[file]`
       - 检查每条更早且未删除的 read：若 `coverage_ratio(old_read.ranges, retained_union(file, after_msg_index=old_read.msg_index, exclude_tool_call_id=old_read.tool_call_id)) >= 0.6` → 标记为待删除（规则 1），然后重建 union
       - 检查 `file_edit_records[file]` 中每条未摘要化 edit：若 `coverage_ratio(edit.hunk_ranges, retained_union(file, after_msg_index=edit.msg_index)) >= 0.6` → 标记该 edit 的 ToolMessage 为待摘要化（规则 2）
     - 对 `name in {"replace", "write"}` 的 tool_call，解析对应 ToolMessage 中的 diff hunk 行范围（从 `@@` header 解析 `+new_start,new_count`）：
       - 记入 `file_edit_records[file]`（hunk 行范围用**新文件行号**，与 `_summarize_edit_diff` 解析逻辑一致）
       - 对 `file_read_records[file]` 中未删除 read 的 `ranges` 做 remap（复用 `_remap_old_range` 逻辑，`file_state.py:156`），按 edit hunk 偏移重映射行号
       - **对 `file_edit_records[file]` 中已有记录的 hunk_range 同样做 remap**（后续 edit 会导致前一个 edit 的 hunk 行号偏移，不做 remap 会让规则 2 的覆盖率计算错位）
       - remap 后范围为空的旧 read → 标记为待删除（整段被删）
       - remap 后范围非空的旧 read → 更新 `ranges` 并保留；edit 阶段不做覆盖率删除
       - 用所有未删除 read records 重建 `file_read_union[file]`
  5. 构建新列表：
     - 跳过待删除的 read tool_call 和 ToolMessage（规则 1）
     - 对被部分移除 tool_call 的 AIMessage，同步更新 `tool_calls`、`additional_kwargs["tool_calls"]` 和 content-list raw `tool_use`
     - 对待摘要化的 edit ToolMessage 用 `model_copy(update={"content": _summarize_edit_diff(...)})` （规则 2）

### `_summarize_edit_diff`

- **Signature**:
  ```python
  def _summarize_edit_diff(content: str) -> str
  ```
- **Input**: `replace` / `write`（insert/append）ToolMessage 的原始 content
- **Output**: 摘要化后的 content
- **行为**:
  1. 保留首行 `File edited: {file} (N operations)`（`replace` 和 `write` 的 insert/append 都走 `edit_execute.py` 的 `_apply_resolved_edits`，output 首行格式一致）
  2. 删除所有以 `@@`、`+`、`-`、` `（diff context 行）开头的行
  3. 从 diff 的 `@@` 行解析改动行号，补 `Changed lines: {start}-{end}`（compile 阶段无 metadata，仅从 diff 文本解析）
  4. 保留 `_line_shift_hints` 产生的提示行（不以 `@@`/`+`/`-` 开头，格式为 `Line shift: ...`）

### read.py 修改

`src/voidx/tools/file_ops/read.py:236-263`，`covered_range is not None` 分支：
- 删除 `note` 变量及其拼接逻辑（239-243 行）
- 直接用 `bounded.output` 作为 output
- `content_budget` 恢复为完整的 `READ_OUTPUT_MAX_CHARS`
- metadata 保留 `already_read: True`（供其他逻辑判断），但内容不加前缀

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| AIMessage 的 tool_calls 缺失或格式异常 | 跳过该 message，不裁剪 |
| tool_call 的 args 缺少 file_path | 跳过该 tool_call，不纳入索引 |
| ToolMessage 找不到配对的 AIMessage tool_call | 跳过，不摘要化 |
| 窗口内行数统计异常（content 非 str） | 按 0 行处理，继续扫描 |
| diff 摘要化时无法解析行号 | 只删 diff 行，不补 `Changed lines` |
| tool_call 的 `name` 是 `file`（create/delete/move） | 不纳入规则 2，diff 完整保留（见 Decisions Log） |

## Testing Strategy

### 规则 1：旧 read 被新 read 覆盖后删除
- 构造 `read A (1-100) → read B (10-30) → read C (40-70) → read D (71-100)`
- 断言：D 到来后 A 的 tool_call 和 ToolMessage 都从 compile 结果中消失；B/C 保留（覆盖率未达标）
- 断言：A 删除后 union 不再包含 A 的区间

### 规则 2：edit diff 被新 read 覆盖后摘要化
- 构造 `edit (hunk 20-31) → read (1-50)`
- 断言：read 到来后 edit 的 ToolMessage content 被替换为 `Changed lines: 20-31 (full content available in recent read of this file)`
- 断言：edit 的 `+`/`-`/`@@` 行被删除，`File edited:` 首行保留

### 规则 2 排除 `file` 工具
- 构造 `file delete (src/foo.py) → read (其他文件)`，使 foo.py 的 delete diff 在窗口内
- 断言：`file` 的 ToolMessage content 不被摘要化，`File deleted:` 首行和 diff 完整保留
- 构造 `file create (src/bar.py, 覆盖已有文件) → read (src/bar.py 1-50)`
- 断言：`file create` 的 diff 不被摘要化（即使新 read 覆盖了 bar.py 的行范围）

### edit remap：小 edit 不杀大 read
- 构造 `read (1-100) → edit (20-25, 净减 2 行) → read (1-20)`
- 断言：edit 时旧 read 做 remap 但不删除（edit 阶段不做覆盖率删除）
- 断言：新 read 到来后按 remapped 范围计算覆盖率，且覆盖 union 排除旧 read 自身
- 断言：若排除自身后的覆盖率 <60%，旧 read 保留

### union 维护：被删 read 的区间移除
- 构造 `read A (1-100) → read B (50-80) → read C (60-70) → read D (50-60)`
- 断言：判定 B 是否可删时，覆盖 union 只包含 B 之后的仍保留 read，并排除 B 自身
- 断言：B 被删后，B 的独占区间（71-80）不再算入 union，不会让别的旧 read 被误判为"已被覆盖"

### 新 read 不被旧大 read 反向删除
- 构造 `read A (1-100) → read B (20-30)`
- 断言：判定 B 是否可删时不能使用 A 作为覆盖证据，B 必须保留
- 再追加 `read C (20-30)` 后，B 可被 C 覆盖删除，A 仍按后续 read 覆盖率独立判定

### 同一 AIMessage 多 tool_call 部分删除
- 构造一个 AIMessage 同时包含 `read foo 1-100`、`read bar 1-20` 两个 tool_call，后续 read 只覆盖 `foo`
- 断言：只删除 `foo` 对应 tool_call 和 ToolMessage，`bar` 的 tool_call 和 ToolMessage 仍保留且 id 配对不变
- 断言：如果 AIMessage 仍有剩余 tool_call，即使文本 content 为空也保留该 AIMessage

### raw provider tool call 同步清理
- 构造 AIMessage 同时包含 `tool_calls`、`additional_kwargs["tool_calls"]`，且 content list 中含 raw `{"type": "tool_use", "id": "call_read"}`
- 触发 `call_read` 删除
- 断言：裁剪后的 AIMessage 中三处都不再包含 `call_read`
- 断言：没有残留的 tool_call id 缺少对应 ToolMessage，也没有残留 ToolMessage 缺少对应 tool_call

### 窗口边界：AIMessage 与 ToolMessage 不被切开
- 构造超长历史（>2000 行），使窗口边界恰好落在某个 AIMessage 与其 ToolMessage 之间
- 断言：该 AIMessage 及其 ToolMessage 整体在窗口内或整体在窗口外，不出现配对断裂

### 窗口外不裁剪
- 构造 `read A → (大量其他 message 填满窗口) → read B`
- 断言：A 在窗口外，不被 B 触发的裁剪逻辑处理

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 惰性裁剪（compile 阶段） | 实时裁剪（执行时改写历史） | 实时裁剪影响不了本次 LLM 请求看到的上下文，且修改不可变历史更危险 |
| 删除 tool_call + ToolMessage | 替换为占位符 | 用户明确要求直接干掉，更省 token |
| 窗口按行数度量 | 按 message 条数 / 按 token 估算 | 行数简单可控，用户指定 |
| edit diff 摘要化（仅被新 read 覆盖时） | 完全保留 / 无条件摘要化 | prune 已清空 input new_string，diff 是唯一内容来源；只有新 read 覆盖了 diff 行范围时才冗余可删 |
| 通过 AIMessage.tool_calls 反查 tool name | 在 ToolMessage 上附加 metadata | ToolMessage 构造时未带 metadata，改构造链路影响面大；反查方式零侵入 |
| already_read 不加提示 | 保留提示 | 提示本身浪费 token，且裁剪逻辑已在 compile 阶段处理冗余 |
| 覆盖判定用后续 read 集合维度（并集） | 单条 vs 单条 / 所有 read 并集 | 实际场景中旧 read 和新 read 都可能是多条的组合，单条对单条会漏判；但所有 read 并集会出现自我覆盖或旧证据删除新证据的问题，因此只使用目标之后的仍保留 read 并集 |
| 覆盖阈值 60%（规则 1 和规则 2 统一） | 100% 包含 / 有交集即触发 | 100% 太保守导致很多该删的没删；有交集太激进会删掉仍有价值的部分；60% 平衡 token 节省和信息保留；规则 1 和规则 2 用同一阈值，逻辑统一 |
| edit 后部分清理（remap + 覆盖率判定） | 全清空并集 / 有交集即删 | edit 只影响改动区域的行号，不受影响的旧 read 行号仍有效；全清空会丢失仍有价值的旧 read；有交集即删太激进（小 edit 杀大 read）；复用现有 `_remap_old_range` 逻辑按 hunk 偏移重映射，删除标准与规则 1 统一走 60% 覆盖率 |
| 规则 2 摘要化删除 diff `-` 行 | 保留 `-` 行 / 只删 `+` 行 | `-` 行代表被删除的旧内容，但 LLM 需要旧内容的场景极少（回滚可重新 read 拿当前状态、edit 意图在 AIMessage 文本推理里不在 `-` 行、多次 edit 演进对比是罕见场景且 compaction 会清理）；旧内容可被重新 read 替代，摘要化删 `-` 行的损失可接受 |
| 规则 2 仅覆盖 `replace`/`write`，排除 `file` 工具 | 一并覆盖 `file` | `file` 的 create/delete/move 走独立路径（`file_ops/file.py`），output 首行是 `File created/deleted/moved:` 而非 `File edited:`，diff 语义是整文件删除/覆盖而非局部编辑；摘要化逻辑（保留 `File edited:` 首行、解析 hunk 行号）对 `file` 不适用，强行套用会产出语义错误的摘要 |

## Open Questions

- [ ] `window_lines` 默认 2000 是否合理？需要实测调参
- [x] edit diff 摘要化后，模型是否还需要看到 diff 的 `+` 行？→ 需要！prune 已清空 input new_string，diff 是唯一内容来源。只有被新 read 覆盖时才摘要化（见 Reader Test 补充细节）
- [x] 多个 edit 操作的 diff 摘要化，`Changed lines` 如何合并多个 hunk 的行号范围？→ 见 Reader Test 补充细节「多 hunk 合并方式」

## Reader Test 补充细节

### 规则 2 触发时机

规则 2 只在**窗口内同一文件出现新 read 且 read 覆盖了 edit diff 的行范围**时触发。

理由（修正）：edit input 的 `new_string` 在下一轮会被 prune 清空（`_prune_ai_tool_call_args`），output diff 是 prune 后唯一的内容来源。因此不能无条件摘要化 diff，只有当新 read 已覆盖 edit 改动的行范围时，diff 才冗余可删。

覆盖判定（基于 edit 之后的已读行区间并集，覆盖率 ≥60% 即触发，与规则 1 一致）：
- 维护文件级的已读行区间并集 `file_read_union`（所有仍保留 read 的范围合并后的不相交区间列表）
- 从 edit diff 的 hunk header 解析 `+new_start,new_count`，得到改动行范围 `[start, end]`（见下方行号解析逻辑）
- 触发条件：`coverage_ratio(edit.hunk_ranges, retained_union(file, after_msg_index=edit.msg_index)) >= 0.6`
- 多 hunk 时，把所有 hunk 的行范围合并为一个集合，计算整体覆盖率

### `_summarize_edit_diff` 行号解析逻辑

unified diff 的 hunk header 格式：`@@ -old_start,old_count +new_start,new_count @@`

解析规则：
- 只取 `+new_start,new_count` 部分（新文件的行号，因为摘要描述的是编辑后的状态）
- `new_count == 0` 时表示纯删除，该 hunk 不产生 `Changed lines`（改动是"删除了旧行"，新文件中无对应行）
- `new_count > 0` 时，改动行范围 = `[new_start, new_start + new_count - 1]`
- `new_count` 省略时默认为 1（如 `@@ -20 +20 @@`）

示例：
- `@@ -20,7 +20,12 @@` → `Changed lines: 20-31`（20 + 12 - 1 = 31）
- `@@ -5,3 +5 @@` → `Changed lines: 5-5`（收缩：3 行变 1 行，`new_count` 省略默认 1，新文件第 5 行）
- `@@ -0,0 +10,3 @@` → `Changed lines: 10-12`（纯插入）

### 多 hunk 合并方式

一个 edit 可能产生多个 hunk（如 `_apply_resolved_edits` 路径）。合并规则：

1. 解析所有 hunk 的 `new_start, new_count`，过滤掉 `new_count == 0` 的
2. 每个有效 hunk 产生一个 `[start, end]` 区间
3. 按区间合并重叠/相邻（`next.start <= cur.end + 1` 则合并，取 `max(end)`）
4. 合并后：
   - 单个区间 → `Changed lines: {start}-{end}`
   - 多个区间 → `Changed lines: {s1}-{e1}, {s2}-{e2}, ...`
   - 空区间（全是纯删除）→ `Changed lines: (deletion only)`，不补行号

示例：
```
@@ -10,3 +10,5 @@    → [10, 14]
@@ -30,2 +32,4 @@    → [32, 35]
```
合并后两个区间不重叠也不相邻 → `Changed lines: 10-14, 32-35`

```
@@ -10,3 +10,5 @@    → [10, 14]
@@ -13,2 +15,3 @@    → [15, 17]
```
合并后 `15 <= 14 + 1` → 合并为 `Changed lines: 10-17`
