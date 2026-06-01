# Thinking 节点点击展开不精准 — 根因分析与修复方案

## 现象

- 点击 "Thinking for Xs" 该行有时无反应
- 点击 Thinking 节点上方某行（不固定），反而意外展开了 Thinking
- 不是点击输入框附近触发的，偏移出现在 thinking 行上方

## 根因

`_render_body()` 中视觉行映射（visual-row → node_id）的计算与实际 prompt_toolkit 折行不一致。

**位置**：`src/voidx/ui/app_parts/rendering.py:349-359`

### 当前代码

```python
for i, line in enumerate(visible):
    vis_w = cell_len(_visible_text(line))
    wraps = max(1, (vis_w + width - 1) // width)   # 问题：全程用全宽算折行
    node_id = line_map.get(start + i)
    for row in range(visual_row, visual_row + wraps):
        row_map[row] = node_id
    visual_row += wraps
```

### 矛盾点

| 谁 | 可用宽度 |
|---|---|
| `_render_body` 手工算折行 | `width`（全宽） |
| prompt_toolkit 实际渲染续行 | `width - len(prefix)`（扣掉 continuation 前缀） |

prompt_toolkit 的 `Window` 在渲染折行时：

- **首行**：用全宽
- **续行（wrap line）**：插入 `_body_line_prefix` 返回的前缀（通常是缩进空格 2~6 字符）
- 续行可用宽度 = `width - 前缀宽度`，更窄 → **实际折行数 > 手工计算折行数**

### 后果

`row_map`（视觉行 → node_id 映射）整体向下偏移——即 node_id 被分配到了比屏幕实际显示更靠上的行号。

当点击 Thinking 节点上方某行时，`_toggle_body_node_at(row)` 查出的 `node_id` 恰好是 Thinking 节点的，于是错误地触发展开/折叠。

## 涉及文件

- `src/voidx/ui/app_parts/rendering.py` — `_render_body()` 方法，第 349-359 行

## 修复方案

改动约 8 行，只改折行计算逻辑，不动数据流或事件处理。

将 `_render_body()` 中的折行计算替换为：

```python
row_map: dict[int, str | None] = {}
visual_row = 0
for i, line in enumerate(visible):
    vis_w = cell_len(_visible_text(line))
    prefix = _continuation_prefix(line)
    prefix_w = len(prefix)
    if vis_w <= width or prefix_w <= 0:
        wraps = max(1, (vis_w + width - 1) // width) if width > 0 else 1
    else:
        cont_width = max(width - prefix_w, 1)
        wraps = 1 + max(0, (vis_w - width + cont_width - 1) // cont_width)
    node_id = line_map.get(start + i)
    for row in range(visual_row, visual_row + wraps):
        row_map[row] = node_id
    visual_row += wraps
self._visible_row_to_node = row_map
```

### 逻辑

- 首行：可用宽度 = `width`（不变）
- 续行：可用宽度 = `width - prefix_w`（扣掉前缀）
- 折行数 = `1 + ceil(max(0, vis_w - width) / cont_width)`
- 若内容不超宽、或无前缀 → 兜底回原公式 `ceil(vis_w / width)`

### 边界情况

| 场景 | 处理 |
|---|---|
| 前缀为空字符串 | 退化为原逻辑，无需特殊处理 |
| 前缀宽度 ≥ width | `cont_width` 保底为 1，保证不除以零 |
| 短行（vis_w ≤ width） | 直接 `wraps = 1`，不走续行计算 |
