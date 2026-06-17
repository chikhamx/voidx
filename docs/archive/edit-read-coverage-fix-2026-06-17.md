# Edit 工具 Read Coverage 改进

> 日期: 2026-06-17
> 状态: 待实施

## 背景

edit 工具要求目标行范围必须被某次 read 完全覆盖，否则报 "Lines X-Y in Z must be read before editing"。LLM 在实际使用中频繁触发此错误，导致反复重试 read→edit，浪费 token 和轮次。

核心代码在 `src/voidx/tools/file_state.py`，关键函数：

- `record_read_range` — 记录一次 read 的行范围
- `covered_read_range` — 检查 edit 目标行是否被已读范围覆盖
- `check_read_coverage` — 覆盖检查的入口，返回错误消息
- `clear_read_coverage` — 清除文件的已读记录

## 问题

### P1: 多次 read 的 ranges 不合并

`record_read_range` 将每次 read 的范围追加到列表，不做合并。`covered_read_range` 要求 edit 范围被**单个** range 完全包含。

LLM 常见模式：分段读大文件（read 1-100，read 101-200），然后 edit 50-150 → 失败，因为没有单个 range 覆盖 50-150。

```python
# file_state.py:59-62 — 只追加，不合并
ctx.file_read_coverage[key] = {
    "fingerprint": fingerprint,
    "ranges": [*ranges, asdict(ReadLineRange(start_line, end_line))],
}
```

```python
# file_state.py:90-91 — 单区间包含检查
for item in ranges:
    if item.get("start_line") <= start_line and end_line <= item.get("end_line"):
```

### P2: write 后清除 coverage

`FileWriteTool` 成功后调用 `clear_read_coverage`，导致 write skeleton → edit anchor 的推荐模式必须中间多一次 read。LLM 经常忘记这一步。

```python
# file_ops.py:168
clear_read_coverage(ctx, path)
```

### P3: edit 后清除 coverage

`FileEditTool` 成功后也调用 `clear_read_coverage`，导致连续多轮 edit 必须每次重新 read。LLM 在 edit 后立即知道文件内容，强制 re-read 是不必要的。

```python
# file_ops.py:284
clear_read_coverage(ctx, path)
```

## 方案

### Fix 1: 合并重叠/相邻 ranges（P1）

在 `record_read_range` 中，插入新 range 后合并重叠和相邻的 ranges。

**改动文件**: `src/voidx/tools/file_state.py`

```python
def _merge_ranges(ranges: list[dict]) -> list[dict]:
    """合并重叠和相邻的行范围。"""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: r["start_line"])
    merged = [sorted_ranges[0].copy()]
    for r in sorted_ranges[1:]:
        last = merged[-1]
        if r["start_line"] <= last["end_line"] + 1:
            last["end_line"] = max(last["end_line"], r["end_line"])
        else:
            merged.append(r.copy())
    return merged

def record_read_range(ctx, resolved, start_line, end_line):
    # ... 现有逻辑 ...
    ctx.file_read_coverage[key] = {
        "fingerprint": fingerprint,
        "ranges": _merge_ranges([*ranges, asdict(ReadLineRange(start_line, end_line))]),
    }
```

**效果**: 分段 read 后可以跨段 edit。`covered_read_range` 无需改动，因为合并后单个 range 就能覆盖跨段区域。

### Fix 2: write 后保留 coverage（P2）

`FileWriteTool` 成功后，不清除 coverage，而是用 write 的内容行数重新记录 coverage（覆盖整个文件）。

**改动文件**: `src/voidx/tools/file_ops.py`

```python
# FileWriteTool.execute 末尾，替换 clear_read_coverage
# 旧: clear_read_coverage(ctx, path)
# 新: 记录 write 内容覆盖的行范围
line_count = len(_split_display_lines(inp.content).lines)
if line_count > 0:
    record_read_range(ctx, path, 1, line_count)
else:
    clear_read_coverage(ctx, path)
```

**效果**: write skeleton 后可以直接 edit anchor，无需中间 read。

**风险**: write 的内容可能很长，但 LLM 刚写了这个内容，它知道文件状态。这与 read coverage 的初衷（确保 LLM 看过文件内容）一致——LLM 自己写的内容当然"看过"。

### Fix 3: edit 后更新 coverage 而非清除（P3）

`FileEditTool` 成功后，根据 edit 操作更新 coverage ranges，而非直接清除。

**改动文件**: `src/voidx/tools/file_state.py` + `src/voidx/tools/file_ops.py`

策略：edit 后重新记录 edit 影响区域的 coverage，并调整后续 ranges 的行号。

简化方案：edit 后对整个文件重新记录 coverage（1 到 total_lines），因为 edit 后 LLM 通过 diff 知道文件状态。

```python
# FileEditTool.execute 末尾，替换 clear_read_coverage
# 旧: clear_read_coverage(ctx, path)
# 新:
new_total = len(lines)
if new_total > 0:
    record_read_range(ctx, path, 1, new_total)
else:
    clear_read_coverage(ctx, path)
```

**效果**: 连续多轮 edit 不需要每次重新 read。

**风险**: 与 Fix 2 相同——LLM 通过 diff 知道 edit 后的文件状态，记录 coverage 是合理的。

## 测试

### Fix 1 测试

```python
def test_merge_overlapping_ranges():
    """分段 read 后可以跨段 edit"""
    # read 1-50, read 40-100 → 合并为 1-100
    record_read_range(ctx, f, 1, 50)
    record_read_range(ctx, f, 40, 100)
    assert covered_read_range(ctx, f, 1, 100) is not None
    assert covered_read_range(ctx, f, 30, 80) is not None

def test_merge_adjacent_ranges():
    """相邻 read 自动合并"""
    record_read_range(ctx, f, 1, 50)
    record_read_range(ctx, f, 51, 100)
    assert covered_read_range(ctx, f, 1, 100) is not None

def test_non_adjacent_ranges_not_merged():
    """不相邻的 range 不合并"""
    record_read_range(ctx, f, 1, 10)
    record_read_range(ctx, f, 20, 30)
    assert covered_read_range(ctx, f, 1, 30) is None  # 11-19 未读
    assert covered_read_range(ctx, f, 1, 10) is not None
```

### Fix 2 测试

```python
def test_write_records_coverage():
    """write 后可以直接 edit，无需重新 read"""
    # 先 read 文件
    record_read_range(ctx, f, 1, 10)
    # write 新内容
    # ... 执行 write ...
    # 应该可以直接 edit
    assert check_read_coverage(ctx, f, 1, new_line_count) is None
```

### Fix 3 测试

```python
def test_edit_preserves_coverage():
    """edit 后可以继续 edit，无需重新 read"""
    # read 文件
    record_read_range(ctx, f, 1, 50)
    # 执行 edit
    # ... 执行 edit ...
    # 应该可以直接再次 edit
    assert check_read_coverage(ctx, f, 1, new_total) is None
```

## 实施顺序

1. Fix 1（range 合并）— 最关键，独立改动最小
2. Fix 2（write 保留 coverage）— 中等改动
3. Fix 3（edit 更新 coverage）— 与 Fix 2 模式相同

每个 fix 独立可测试，可分批实施。
