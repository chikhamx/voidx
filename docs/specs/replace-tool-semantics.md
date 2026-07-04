# replace 工具语义重整 — 技术设计文档

## Context

`replace` 工具目前使用 `start_no` / `end_no` / `start_anchor` / `end_anchor` 四个字段描述替换边界。这个模型能表达能力，但对调用方不够直观，尤其是空 anchor 的语义容易被误解：

1. **单行 replace**：空 anchor 实际表示“信任行号，不校验 anchor”。
2. **多行 replace**：空 anchor 目前会被当成“匹配空行”，但这不是调用方直觉，且如果改成 wildcard 会严重削弱多行替换的定位约束。
3. **field 描述不够硬**：没有明确告诉调用方多行替换必须提供首尾行的非空 anchor。
4. **new_string 语义缺失**：trailing newline 和 head/tail dedup 行为会影响最终文件内容，但描述里没有说明。

本次设计目标是把边界表达改得更清楚，同时保持多行替换安全：**单行允许空 anchor；多行必须提供非空 anchor**。

## Goals and Non-Goals

### Goals

- 将 replace 入参改为对象数组边界模型，减少 `start_*` / `end_*` 字段的重复和对称误导。
- 明确单行 replace：只传一个边界；`anchor` 可为空，空则不校验 anchor，直接按 `line_no` 替换该行。
- 明确多行 replace：传两个边界；两个 `anchor` 都必须非空，分别校验替换范围首行和末行。
- 避免把多行空 anchor 改成 wildcard，防止现有多行 replace 定位约束被削弱。
- 补充 `new_string` 的 trailing newline 与 head/tail dedup 描述。
- 更新 tool description、parameter schema、validation 和测试。

### Non-Goals

- 不改变 anchor 搜索半径（`TEXT_REPLACE_LINE_RADIUS = 3`）。
- 不改变 drift fallback 机制。
- 不改变 read coverage 校验逻辑。
- 不改变 head/tail dedup 代码行为本身，只在描述中说明。
- 不支持 0 个或 3+ 个替换边界。
- 不让多行 replace 的空 anchor 匹配任意行。

## Architecture

### 新语义模型

`replace` 的核心操作仍然是用 `new_string` 替换文件中的 whole-line 范围。新接口用 `bounds` 描述替换边界：

```json
{
  "file_path": "src/foo.py",
  "bounds": [
    {"line_no": 10, "anchor": "def old_func"},
    {"line_no": 18, "anchor": "return result"}
  ],
  "new_string": "..."
}
```

- `bounds.length == 1`：单行 replace，替换 `bounds[0].line_no` 这一行。
- `bounds.length == 2`：多行 replace，两个 bound 的传入顺序不重要；较小的 `line_no` 作为起始边界，较大的 `line_no` 作为结束边界。
- `bounds.length` 为其他值：validation error。
- 两个 bound 的 `line_no` 相同：validation error；单行替换应只传一个 bound。

命名选择 `bounds`，因为它表达的是替换范围的边界，而不是任意目标列表。相比 `targets`，`bounds` 更能约束调用方理解：一个 boundary 表示单行，两个 boundaries 表示范围首尾。两个 boundaries 是无序边界点，不要求调用方按起止顺序传入。

### Anchor 规则

| 模式 | bounds 数量 | anchor 是否可空 | 行为 |
|------|-------------|----------------|------|
| 单行 replace | 1 | 可空 | 空 anchor 不校验内容，直接按 `line_no` 定位 |
| 单行 replace | 1 | 非空 | 在 `line_no` 附近搜索包含 anchor 的单行 |
| 多行 replace | 2 | 不可空 | 先按 `line_no` 排序，再用首尾 anchor 校验范围首行和末行 |
| 多行 replace | 2 | 任一为空 | validation error |

多行 replace 禁止空 anchor 是本设计的核心安全约束。这样可以保留当前多行替换“必须有首尾内容锚点”的定位强度，避免空字符串扩大候选集合、引入歧义或误替换。

Anchor 不支持跨行：多行 anchor（含 `\n`）只取第一个非空行作为匹配片段，这一点保持现有实现语义。

## Data Model

### 新入参模型

```python
class ReplaceBound(BaseModel):
    line_no: int = Field(
        ge=1,
        description="Line number (1-based) from the latest read output."
    )
    anchor: str = Field(
        description=(
            "Substring expected on this boundary line. For single-line replace, "
            "empty anchor skips anchor validation and uses line_no directly. "
            "For multi-line replace, anchor must be non-empty. Does not span lines — "
            "a multi-line anchor uses only its first non-empty line."
        )
    )

class FileReplaceInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    bounds: list[ReplaceBound] = Field(
        min_length=1,
        max_length=2,
        description=(
            "Replacement boundary lines. Provide one bound for single-line replace, "
            "or two unordered bounds for multi-line replace. In multi-line replace, "
            "both anchors must be non-empty; the smaller line_no is used as the start "
            "boundary and the larger line_no is used as the end boundary."
        )
    )
    new_string: str = Field(
        description=(
            "Content that replaces the selected whole line or line range. Empty string "
            "deletes the selected lines. The replacement string's trailing newline is "
            "ignored for line splitting; the original file's trailing-newline state is "
            "preserved unless the file becomes empty. If the first or last line of "
            "new_string exactly matches the line immediately before or after the replaced "
            "range, that adjacent line is also consumed."
        )
    )
```

### Compatibility mapping

Internally, the new shape maps to the existing resolver model:

```python
if len(bounds) == 1:
    start_no = end_no = bounds[0].line_no
    start_anchor = end_anchor = bounds[0].anchor
else:
    first, second = sorted(bounds, key=lambda bound: bound.line_no)
    start_no = first.line_no
    end_no = second.line_no
    start_anchor = first.anchor
    end_anchor = second.anchor
```

Additional validation:

```python
if len(bounds) == 2:
    if bounds[0].line_no == bounds[1].line_no:
        raise ValueError("two-bound replace requires different line_no values; use one bound for single-line replace")
    if bounds[0].anchor == "" or bounds[1].anchor == "":
        raise ValueError("multi-line replace requires non-empty anchors for both boundary lines")
```

For `len(bounds) == 1`, empty anchor is valid and intentionally maps to the existing single-line direct line-number path. For `len(bounds) == 2`, caller order is not significant: the smaller `line_no` becomes the start boundary and the larger `line_no` becomes the end boundary.

### FileReplaceTool description

```text
Replace whole lines in a file. Provide one bound for single-line replace or two unordered bounds for multi-line replace. Read the target lines first. Single-line replace may use an empty anchor to trust line_no directly. Multi-line replace requires non-empty anchors on both boundary lines; the smaller line_no is used as the start boundary and the larger line_no is used as the end boundary. Anchors are searched near the given line numbers in case the file changed since the last read.
```

## API Contract

### Request examples

#### Single-line replace with anchor validation

```json
{
  "file_path": "src/foo.py",
  "bounds": [{"line_no": 42, "anchor": "old_value ="}],
  "new_string": "new_value = 1"
}
```

#### Single-line replace without anchor validation

```json
{
  "file_path": "src/foo.py",
  "bounds": [{"line_no": 42, "anchor": ""}],
  "new_string": "new_value = 1"
}
```

#### Multi-line replace

```json
{
  "file_path": "src/foo.py",
  "bounds": [
    {"line_no": 42, "anchor": "def old_func"},
    {"line_no": 47, "anchor": "return value"}
  ],
  "new_string": "def new_func():\n    return value"
}
```

#### Multi-line replace with reversed bounds

```json
{
  "file_path": "src/foo.py",
  "bounds": [
    {"line_no": 47, "anchor": "return value"},
    {"line_no": 42, "anchor": "def old_func"}
  ],
  "new_string": "def new_func():\n    return value"
}
```

The reversed request is equivalent to the ordered request: line 42 is used as the start boundary and line 47 is used as the end boundary.

#### Invalid multi-line replace

```json
{
  "file_path": "src/foo.py",
  "bounds": [
    {"line_no": 42, "anchor": ""},
    {"line_no": 47, "anchor": "return value"}
  ],
  "new_string": "..."
}
```

Expected error:

```text
multi-line replace requires non-empty anchors for both boundary lines
```

#### Invalid duplicate bounds

```json
{
  "file_path": "src/foo.py",
  "bounds": [
    {"line_no": 42, "anchor": "old_value ="},
    {"line_no": 42, "anchor": "old_value ="}
  ],
  "new_string": "..."
}
```

Expected error:

```text
two-bound replace requires different line_no values; use one bound for single-line replace
```

## Implementation Plan

### 1. Update input schema

File: `src/voidx/tools/file_ops/edit_execute.py`

- Add `ReplaceBound` model.
- Replace `start_no`, `end_no`, `start_anchor`, `end_anchor` fields with `bounds`.
- Add model validation for `bounds.length`, duplicate two-bound line numbers, and multi-line non-empty anchors.
- Sort two-bound requests by `line_no`, then map `bounds` into existing `_execute_text_replace(...)` arguments before execution.

### 2. Preserve resolver semantics

File: `src/voidx/tools/file_ops/edit_resolve.py`

- Keep `_line_matches_replace_anchor("")` behavior unchanged: empty snippet matches only empty lines in generic resolver paths.
- Keep `_find_single_line_segment` empty `prefix` behavior unchanged: single-line empty anchor trusts `line_no` directly.
- Keep multi-line empty anchor behavior unreachable through validation, except for defensive internal calls.
- Keep `suffix != prefix` shortcut unless a separate design explicitly changes duplicate-line behavior.

### 3. Update visible descriptions

File: `src/voidx/tools/file_ops/edit_execute.py`

- Update `FileReplaceTool.description` to describe unordered `bounds`.
- Update field descriptions to explain single-line vs multi-line anchor rules.
- Ensure descriptions avoid old parameter names except in migration comments/tests.

### 4. Update tests

File: `tests/test_tools/test_file_ops_edit.py`

- Update existing replace calls to use `bounds`.
- Update schema/description tests for the new parameter shape.
- Preserve tests proving single-line empty anchor trusts `line_no`.
- Preserve or add tests proving multi-line empty anchor is rejected by validation.
- Add tests proving reversed two-bound order is accepted and normalized by `line_no`.
- Add tests proving duplicate two-bound `line_no` values are rejected.
- Preserve trailing-newline and dedup tests; update only request payload shape.

## Error Handling

| Failure | Expected behavior |
|---------|-------------------|
| `bounds` is empty | validation error: provide one or two bounds |
| `bounds` has 3+ items | validation error: provide one or two bounds |
| multi-line bound has empty anchor | validation error before resolver |
| two multi-line bounds are provided out of order | valid; bounds are sorted by `line_no` before resolver mapping |
| two bounds use the same `line_no` | validation error: use one bound for single-line replace |
| single-line empty anchor line is out of range | existing line out-of-range error |
| single-line non-empty anchor missing near line | existing anchor-not-found message |
| multi-line non-empty anchors produce ambiguous range | existing ambiguous range message |

## Test Impact

### Tests to update structurally

All direct `replace` tool invocations currently passing:

```json
{
  "start_no": 2,
  "end_no": 3,
  "start_anchor": "body",
  "end_anchor": "end"
}
```

should become:

```json
{
  "bounds": [
    {"line_no": 2, "anchor": "body"},
    {"line_no": 3, "anchor": "end"}
  ]
}
```

Single-line calls should become:

```json
{
  "bounds": [{"line_no": 2, "anchor": "body"}]
}
```

### Tests to preserve semantically

| Test area | Expected outcome |
|-----------|------------------|
| Single-line empty anchor | Still succeeds and trusts `line_no` |
| Single-line nearby empty line regression | Still replaces the requested line, not a nearby empty line |
| Multi-line empty anchor | Now fails validation with a clear non-empty-anchor message |
| Reversed multi-line bounds | Succeeds; smaller `line_no` is start and larger `line_no` is end |
| Duplicate two-bound line numbers | Fails validation; use one bound for single-line replace |
| Duplicate single-line anchors | Existing `suffix == prefix` behavior remains unchanged |
| Trailing newline | Replacement string trailing newline does not add blank lines |
| Head/tail dedup | Adjacent duplicate first/last replacement lines are still consumed |

## Migration Notes

This is a breaking schema change for the `replace` tool. If backward compatibility is required, add a temporary compatibility parser that accepts either shape:

- New shape: `bounds`
- Old shape: `start_no`, `end_no`, `start_anchor`, `end_anchor`

If compatibility mode is added, old-shape multi-line calls with empty anchors should be rejected with the same validation message as new-shape calls.

## Decisions Log

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| Use `bounds` object array | `targets`, `anchors`, tuple array | `bounds` best communicates range boundaries and avoids position-only tuple ambiguity |
| One bound means single-line replace | Keep start/end fields | Removes duplicate single-line fields and makes empty-anchor shortcut explicit |
| Two bounds means multi-line replace | Support arbitrary count | Replace operates on one contiguous range, so only one or two bounds are valid |
| Two-bound order is not significant | Require caller to pass start before end | Object arrays describe boundary points, not ordering; sorting by `line_no` makes reversed bounds safe and deterministic |
| Equal two-bound line numbers are invalid | Treat as single-line replace | Single-line replace already has the one-bound form; rejecting duplicates avoids ambiguous anchor semantics |
| Multi-line anchors must be non-empty | Empty anchor matches any line | Wildcard empty anchors weaken range定位 and create too much ambiguity |
| Preserve resolver empty-string matching | Change `_line_matches_replace_anchor("")` to wildcard | Avoids broad behavioral changes outside the single-line direct path |
| Keep `suffix != prefix` shortcut | Recheck identical suffix | Avoids changing duplicate-line behavior as part of this schema/design change |

## Open Questions

- [ ] 是否需要短期兼容旧 schema，还是直接 breaking change？
- [ ] 错误消息是否需要提到旧字段迁移到 `bounds`，帮助调用方修正请求？
