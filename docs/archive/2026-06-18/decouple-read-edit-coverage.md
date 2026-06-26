# Spec: 精确 Read Coverage 与锚点编辑

> **Status: Done**

## 背景

`file_read_coverage` 现在同时承担两个职责：

1. **防重复读**：`read` 工具检查当前文件版本的某些行是否已经返回给 LLM，避免重复输出。
2. **编辑许可**：`edit` 工具要求 LLM 先读过目标行，避免盲目按行号修改。

当前 `edit` 成功后会调用 `record_read_range(ctx, path, 1, new_total)`，把整个新文件标记为已读。这个行为过宽：LLM 只看到了 edit 返回的 diff，却没有看到 diff 外的文件内容。之后如果再 `read` 未展示过的行，`read` 会因为 coverage 命中而跳过，导致 LLM 拿不到真实内容。

同时，`edit` 是纯行号定位。插入或删除导致行号偏移后，LLM 可能继续使用旧行号；只要旧行号仍在文件范围内，工具会静默改错位置。

## 目标

本实现一次完成两件事：

1. **精确更新 `edit` 后的 read coverage**：只保留仍然可信的旧 coverage，并把 edit diff 中真正展示给 LLM 的新文件行加入 coverage。
2. **加入锚点编辑校验**：让 `edit` 可选接收 `scope` 和 `anchor`，在行号漂移时校验或修正目标行，把静默错改变成显式纠正或显式错误。

`write` 仍然保持现有语义：LLM 提交了完整 `content`，所以写入成功后可以把新文件内容视为已知内容并记录全量 coverage。这个 spec 不把 `write` 改成基于 unified diff 的 coverage，因为当前 diff 是上下文 diff，不保证包含整个新文件。

## 非目标

- 不改变 `read` 的防重复读行为。
- 不改变 LSP format 的 coverage 处理；LSP format 继续 `clear_read_coverage`，后续单独优化。
- 不引入语言 AST 解析。`scope` 和 `anchor` 都是纯文本包含匹配。
- 不新增独立的 `file_edit_coverage`。同一个 `file_read_coverage` 继续服务防重复读和编辑许可，只是记录更精确。

## 当前实现约束

相关现状：

- `make_file_diff()` 返回 unified diff 字符串，不是 `FileDiff` 对象。
- `parse_unified_diff()` 可以把 diff 字符串解析成 `StructuredDiff -> FileDiff -> DiffHunk -> DiffLine`。
- `ToolResult.diff` 类型是 `str | None`。
- `edit` 现在在写入后更新 coverage；实现新 remap 时必须先捕获旧 coverage ranges，再写入新 fingerprint。

因此新 helper 不直接依赖 `ToolResult.diff`，而是在 `FileEditTool.execute()` 内部按以下顺序处理：

1. 读取旧文件内容。
2. 校验 staleness 和 edit 形状：`replace` 需要 `end_line >= start_line`，`insert` 不带 `end_line`，`insert` 允许 `start_line=0` 表示插入文件开头。
3. 应用 anchor/scope 定位，得到最终行号。
4. 所有 edit 完成定位后，整体重跑 line range、overlap、duplicate insertion、insertion-inside-replacement 校验。
5. 对修正后的目标行检查 read coverage。
6. 捕获写入前的旧 coverage ranges。
7. 应用 edits，生成新内容。
8. 保存文件并生成 diff 字符串。
9. `parse_unified_diff(diff_text)` 得到 `FileDiff`。
10. 用旧 coverage ranges + parsed diff remap coverage。
11. 用新 fingerprint 写回 `file_read_coverage`。

## Coverage Remap 设计

核心原则：**edit 返回的 diff 是 LLM 在新文件版本中实际看到的内容**。diff 里的 `add` 和 `context` 行具有 `new_lineno`，这些新文件行可以记为已读；diff 外的行只有在它们原本已读且内容未变时，才可以继续保留 coverage。

### 更新规则

对每次 edit：

- **hunk 之前的旧 coverage**：内容没变，行号不变，保留。
- **hunk 范围内的旧 coverage**：内容可能变化，丢弃；再由 diff 中的 `add/context` 新行重新覆盖。
- **hunk 之后的旧 coverage**：内容没变，但行号可能偏移，按累计 offset 调整。

每个 hunk 的 offset 为 `new_count - old_count`。多个 hunk 按 old 行号升序处理，hunk 后方 coverage 使用之前所有 hunk 的累计 offset。

### Helper 接口

新增 helper 放在 `src/voidx/tools/file_state.py`：

```python
def remap_read_coverage_from_file_diff(
    ctx: ToolContext,
    resolved: Path,
    file_diff: FileDiff,
    *,
    old_ranges: list[dict],
) -> None:
    """Remap old read coverage through an edit diff and write coverage for the new file version."""
```

调用方负责传入写入前捕获的 `old_ranges`。helper 内部只使用当前文件 fingerprint 写回新 coverage。

需要一个小的内部结构：

```python
@dataclass(frozen=True)
class DiffSpan:
    old_start: int
    old_end: int
    offset: int
```

`old_end = old_start + old_count - 1`。对纯新增 hunk，如果 unified diff 出现 `old_count == 0`，实现要把旧范围视为空区间，不能错误丢弃旧 coverage。

### Remap 算法

输入：

- 写入前旧 coverage ranges。
- parsed `FileDiff.hunks`。
- 写入后的真实文件 fingerprint。

输出：

- 新文件版本的 coverage ranges。

步骤：

1. 从 hunks 提取 `DiffSpan`，并按 `old_start` 排序。
2. 对每个旧 coverage range `[start, end]`，按 hunk 边界切分。
3. 位于 hunk 前的片段保留当前累计 offset 后的位置。
4. 与 hunk 旧范围相交的片段丢弃。
5. hunk 后的片段继续处理，并在跨过 hunk 后增加累计 offset。
6. 从 diff lines 收集 `kind in {"add", "context"}` 且 `new_lineno is not None` 的行，压缩成 ranges。
7. 合并 remapped old ranges 和 diff-visible ranges，写入新 fingerprint。

如果 diff 为空，说明 edit 没有改变文件内容。此时不做 diff remap，直接用当前文件 fingerprint 刷新旧 coverage ranges，避免 no-op 写入导致 coverage fingerprint 过期。

### 关键示例

**部分读后编辑，不污染未读行**

```
read 1-2, coverage [1-2]
edit line 2, diff 展示 new lines 1-3
coverage -> [1-3]
read line 5 -> 正常返回，因为 line 5 未覆盖
```

**删除行后保留后方已读内容**

```
read 1-100, coverage [1-100]
delete line 50, hunk old 47-53 new 47-52 offset=-1
旧 coverage:
  [1-46] 保留
  [47-53] 丢弃
  [54-100] 偏移为 [53-99]
diff-visible:
  [47-52]
合并 -> [1-99]
```

**多 hunk 偏移抵消**

```
read 1-100, coverage [1-100]
hunk 1: old 7-13, new 7-14, offset=+1
hunk 2: old 47-53, new 48-53, offset=-1
coverage -> [1-100]
```

这里最终覆盖仍是 `[1-100]`，不是因为整文件被强行标记已读，而是因为旧 coverage 全文件已读，两个 hunk 的新内容也都通过 diff 展示。

## Write Coverage 设计

`write` 不使用 diff remap。原因：

- `make_file_diff()` 生成的是上下文 unified diff，已有文件被整体覆盖时 diff 不一定包含全部新行。
- `write` 的 `content` 是 LLM 本轮提交的完整内容，按工具契约可视为 LLM 已知。

`FileWriteTool` 保持：

- 写入非空文件后 `record_read_range(ctx, path, 1, line_count)`。
- 写入空文件后 `clear_read_coverage(ctx, path)`。

可以补一条回归测试明确这个行为，避免后续误把 write 切到 diff remap。

## 锚点编辑设计

`EditEntry` 新增两个可选字段：

```python
class EditEntry(BaseModel):
    operation: Literal["replace", "insert"]
    start_line: int
    end_line: int | None = None
    new_string: str
    scope: str | None = Field(
        default=None,
        description=(
            "Optional scope anchor. Copy a nearby function/class definition or other stable "
            "line from a prior read output. Used to limit anchor search when line numbers drift."
        ),
    )
    anchor: str | None = Field(
        default=None,
        description=(
            "Optional expected content at start_line. If start_line no longer contains this text, "
            "the edit tool searches for it and either corrects the line or returns an ambiguity error."
        ),
    )
```

### 匹配规则

- `scope` 使用 `scope in line_content`。
- `anchor` 使用 `anchor in line_content`。
- 不 trim、不模糊匹配、不正则匹配。
- 多个匹配要报错并列出匹配行号。
- 未找到要报错并建议重新 read。

### 定位流程

对每个 edit 独立处理：

1. 如果没有 `scope` 和 `anchor`，保持当前纯行号行为。
2. 如果有 `scope`，在全文件搜索 scope。
   - 唯一匹配：得到 `scope_line`。
   - 多个匹配：返回错误，列出行号。
   - 未找到：返回错误。
3. 如果有 `anchor`，先校验当前 `start_line` 是否包含 anchor。
   - 匹配：使用原行号。
   - 不匹配且有 scope：从 `scope_line` 附近开始，在文件中搜索 anchor。
   - 不匹配且无 scope：全文件搜索 anchor。
4. anchor 搜索唯一命中时，修正 `start_line`。
   - `replace` 且 `end_line` 原本与 `start_line` 相同：同步修正为新 start line。
   - `replace` 多行范围：按原长度平移 `end_line`。
   - `insert`：修正为 anchor 行，并在该行之后插入。
5. 所有 edit 自动修正后，必须整体重跑 line range、overlap、duplicate insertion、insertion-inside-replacement 校验，再做 read coverage check。

### Scope 附近搜索

第一版不引入 AST，但要做文本级 block window 推断，避免“scope 找到了但 anchor 跑到别的函数”：

- 有 scope 时，anchor 搜索范围从 `scope_line` 开始。
- 如果 scope 行以 `:` 结尾，按缩进块推断边界：从下一行开始，遇到缩进小于等于 scope 行缩进的非空行时结束。
- 如果 scope 行包含未闭合的 `{`，按花括号平衡推断边界，直到对应 `}` 结束。
- 如果两种规则都无法应用，退化为到下一个同等或更小缩进且看起来像结构起点的非空行之前；结构起点包括 `def `、`class `、`async def `、`function `、`fn `、`struct `、`enum `、`interface `、`type `。
- 如果仍无法可靠推断边界，退化为从 `scope_line` 到文件末尾。
- 如果该范围内有多个 anchor 匹配，返回歧义错误。

这个策略仍是文本级，不引入语言解析。

### 插入语义

`edit` 只保留一个插入操作：

- `operation="insert"` 表示在 `start_line` 之后插入 `new_string`。
- `start_line=0` 只对 `insert` 有效，表示插入文件开头。
- `insert` 不接受 `end_line`。
- `insert` 的 `anchor` 校验 `start_line` 的内容；`start_line=0` 时不能提供 `anchor`，因为没有 anchor 行。
- 同一个 `start_line` 上的多个 `insert` 仍然视为歧义并拒绝。

### 修正反馈

如果工具自动修正行号，`edit` 输出在 diff 前追加一行：

```
Line corrected: edit 0 start_line 5 -> 6 (matched anchor 'def bar():')
```

如果没有修正，不额外输出。

## 行号偏移提示

`edit` 导致行数变化时，应在输出中提示行号偏移。这个提示不能只从 unified diff hunk header 推导，因为 hunk header 包含上下文行，`old_start + old_count - 1` 不是实际编辑边界。

提示从最终应用的 `EditEntry` 推导：

- `replace`：`offset = len(new_lines) - (end_line - start_line + 1)`。
  - 若 `offset != 0`：`Line shift: lines after {end_line} shifted by {offset:+d}`。
- `insert`：`offset = len(new_lines)`。
  - `Line shift: lines after {start_line} shifted by +N`。
  - 当 `start_line=0` 时，提示为 `Line shift: all existing lines shifted by +N`。

多 edit 时按最终行号升序输出提示。提示中的行号使用 anchor 修正后的最终行号。

## 工具行为变化

### read

无行为变化。

### write

无行为变化；补测试锁定现有契约。

### edit

变化：

- 支持可选 `scope` / `anchor`。
- 插入操作收敛为 `operation="insert"`，默认在 `start_line` 之后插入；`start_line=0` 插入文件开头。
- 成功后不再全量 `record_read_range(1, new_total)`。
- 成功后用 parsed diff 精确 remap coverage。
- 行数变化时输出 line shift hints。
- anchor 自动修正时输出 line corrected hint。
- 更新 `edit` tool description 和 schema descriptions，鼓励模型在行号可能漂移时提供 `anchor`，并说明 `insert` 的 after-line 语义。

错误优先级：

1. path/staleness/空 edits/空文件等现有错误。
2. 初始 line range 形状错误。
3. scope/anchor 定位错误。
4. 修正后的 line range 错误。
5. read coverage 错误。

## 文件清单

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/file_state.py` | 新增 `remap_read_coverage_from_file_diff` 及内部 range remap helpers |
| `src/voidx/tools/file_ops.py` | `EditEntry` 将插入操作收敛为 `insert`，新增 `scope`/`anchor`；edit 执行前做锚点定位；edit 成功后 parse diff 并 remap coverage；输出 corrected/shift hints；更新 tool description |
| `tests/test_tools/test_basic.py` | 增加 coverage remap、write 契约、anchor/scope、line shift hint 测试 |

## 测试计划

### Coverage remap

1. `read 1-10 -> replace line 5`：coverage 仍覆盖 1-10。
2. `read 1-30 -> expand line 5`：coverage 覆盖 1-31，后续未读行不被误拦截。
3. `read 1-100 -> delete line 50`：coverage 覆盖 1-99。
4. `read 1-2 -> edit line 2 -> read line 5`：read 正常返回内容。
5. `read 1-2 -> edit line 2 -> read line 2`：read 被 already-read 拦截，因为 diff 展示过该行。
6. `read 1-2 -> edit line 2 -> edit line 5`：第二次 edit 因 coverage 不足被拒绝。
7. 多 hunk 同时编辑：coverage 正确切分并合并。
8. no-op edit 或空 diff：旧 coverage 刷新 fingerprint 后保持，不丢失。

### Write 契约

9. `read line 1 -> write 全新内容 -> edit 新 line 1`：允许。
10. `write` 大文件后 coverage 覆盖全部新内容，明确不依赖 diff hunks。
11. `write` 空文件后 coverage 清空。

### Anchor/scope

12. `anchor` 匹配当前 `start_line`：正常编辑。
13. `anchor` 不匹配但全文件唯一匹配：自动修正行号并编辑。
14. `anchor` 不匹配且多个匹配：返回歧义错误，不写文件。
15. `anchor` 未找到：返回错误，不写文件。
16. `scope` 唯一匹配且 anchor 在 scope 范围内唯一匹配：自动修正。
17. `scope` 未找到：返回错误。
18. `scope` 多个匹配：返回歧义错误。
19. `replace` 多行范围修正后保持原范围长度。
20. `insert` 使用修正后的 anchor 行，并在该行之后插入。
21. 自动修正后目标行未被 read coverage 覆盖：返回 coverage 错误，不写文件。
22. 两个 edit 经 anchor 修正后重叠或落到同一 insertion anchor：返回冲突错误，不写文件。
23. `insert start_line=0`：在文件开头插入，不要求 read coverage。

### Hints

24. delete/expand/insert 输出 line shift hint。
25. 行数不变的 replace 不输出 line shift hint。
26. anchor 修正输出 `Line corrected`。
27. 多 edit 输出多条 shift hint，行号基于修正后的最终 edit。
28. `insert start_line=0` 输出 all existing lines shifted hint。

## 实现顺序

1. 先写 coverage remap 单元/工具测试，覆盖部分读、expand、delete、多 hunk、no-op。
2. 实现 `remap_read_coverage_from_file_diff`，让现有 edit 成功后使用 parsed diff remap。
3. 补 write 契约测试，确认 write 不走 diff remap。
4. 写 anchor/scope 失败和修正测试。
5. 实现 anchor/scope 定位与修正。
6. 写 hints 测试。
7. 实现 line corrected 和 line shift hints。
8. 跑 focused tests：`.venv/bin/python -m pytest tests/test_tools/test_basic.py -v`。
