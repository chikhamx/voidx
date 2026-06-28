> **Status: Done**

# 行号漂移回退匹配 — 实现计划

> 依据设计文档：`docs/archive/2026-06-28/line-drift-fallback.md`

## Goal

edit 工具在 `_find_text_segment` anchor 搜索失败时，利用 runtime 维护的 read epoch → 当前文件行号映射重试匹配，避免 LLM 因行号漂移被迫重新 read。

## Architecture

在 `file_read_coverage[key]` 中新增可选字段 `line_drift_maps`，按 read epoch 存储行号漂移的 step 序列。`replace` 路径在 anchor 搜索失败时，遍历候选 drift maps 换算行号后重试 `_find_text_segment`，要求唯一命中。漂移换算复用现有 `_remap_old_range` 语义，保证与 coverage 重映射同源。

## Tech Stack

- Python 3.11+，dataclass / NamedTuple
- 现有模块：`voidx.tools.file_state`、`voidx.tools.file_ops.edit_resolve`、`voidx.tools.file_ops.edit_execute`、`voidx.tools.file_ops.types`
- 测试：pytest + asyncio（沿用 `tests/test_tools/test_file_ops_edit.py` 风格）

## File Structure

| 文件 | 责任 |
|------|------|
| `src/voidx/tools/file_state.py` | 新增 `LineDriftMap` dataclass、`get_line_drift_maps`；改造 `record_read_range` / `remap_read_coverage_from_file_diff` 为保留式写入并维护 `line_drift_maps`；新增 `MAX_LINE_DRIFT_MAPS_PER_FILE` 常量与 FIFO 淘汰 |
| `src/voidx/tools/file_ops/edit_resolve.py` | 新增 `remap_line_range` 纯函数（复用 `_remap_old_range` 语义） |
| `src/voidx/tools/file_ops/edit_execute.py` | 新增 `DriftFallbackResult` NamedTuple、`_find_text_segment_with_drift_fallback`；替换 `_execute_text_replace` 中 :128 对 `_find_text_segment` 的直接调用；回退命中时追加提示到 output |
| `tests/test_tools/test_file_ops_edit.py` | 新增漂移回退端到端测试（通过 ToolRegistry 驱动） |
| `tests/test_tools/test_file_ops_coverage_fingerprint.py` | 新增 `line_drift_maps` 生命周期与 `remap_line_range` 单元测试 |

## Tasks

### Task 1: `LineDriftMap` 模型与常量（file_state.py）

- [ ] 1.1 在 `file_state.py` 新增 `MAX_LINE_DRIFT_MAPS_PER_FILE = 16` 常量（放在 `DiffSpan` 定义附近）
- [ ] 1.2 新增 `@dataclass(frozen=True) LineDriftMap`：`epoch: int`、`source_ranges: list[ReadLineRange]`、`span_steps: list[list[DiffSpan]]`
- [ ] 1.3 新增 `_line_drift_maps_from_raw(raw: list[dict]) -> list[LineDriftMap]`：把 dict 列表解析为 `LineDriftMap`，span_steps 内的 dict 还原为 `DiffSpan`
- [ ] 1.4 新增 `_line_drift_maps_to_raw(maps: list[LineDriftMap]) -> list[dict]`：反向序列化

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_coverage_fingerprint.py -k "line_drift_map_model" -v` — 验证 round-trip 序列化保持 epoch/source_ranges/span_steps。

### Task 2: `record_read_range` 改为保留式写入（file_state.py）

- [ ] 2.1 读取既有 `line_drift_maps`：fingerprint 匹配则保留，不匹配（视为新文件）则清空为 `[]`
- [ ] 2.2 生成新 epoch：`max([m.epoch for m in existing_maps], default=0) + 1`
- [ ] 2.3 追加新 map：`LineDriftMap(epoch=新epoch, source_ranges=[ReadLineRange(start_line, end_line)], span_steps=[])`
- [ ] 2.4 若 maps 数量超过 `MAX_LINE_DRIFT_MAPS_PER_FILE`，按 epoch 升序丢弃最小的（FIFO）
- [ ] 2.5 写回 dict 时保留 `line_drift_maps` 字段（连同 `fingerprint` + `ranges`）

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_coverage_fingerprint.py -k "record_read_range_preserves" -v` — 验证：(a) 首次 read 后 maps 有 1 个 epoch=1 的空 step map；(b) 第二次 read 追加 epoch=2 且保留 epoch=1；(c) fingerprint 不匹配时清空旧 maps。

### Task 3: `remap_read_coverage_from_file_diff` 追加 step（file_state.py）

- [ ] 3.1 读取既有 `line_drift_maps`（fingerprint 匹配时；不匹配时为 `[]`）
- [ ] 3.2 把本次 `file_diff` 构造的 `spans`（已有逻辑 :119-126）作为一个新 step，append 到每个 map 的 `span_steps`
- [ ] 3.3 写回时连同重映射后的 `ranges` 一起保留 `line_drift_maps`
- [ ] 3.4 `ranges` 为空走 `clear_read_coverage` 分支时，整个 key 删除（`line_drift_maps` 一并丢失，符合设计）

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_coverage_fingerprint.py -k "remap_appends_step" -v` — 验证：(a) edit 后每个 map 的 span_steps 长度 +1；(b) 多次 edit 不 read 时 step 序列累积；(c) 第二个 step 的坐标系为"第一次 edit 后"。

### Task 4: `get_line_drift_maps`（file_state.py）

- [ ] 4.1 新增 `get_line_drift_maps(ctx, resolved) -> list[LineDriftMap]`：从 `file_read_coverage[key]["line_drift_maps"]` 解析，不存在则返回 `[]`
- [ ] 4.2 不做独立指纹校验（依赖 staleness 防线）

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_coverage_fingerprint.py -k "get_line_drift_maps" -v` — 验证：(a) 未追踪文件返回 `[]`；(b) read 后返回非空列表；(c) 返回的 `LineDriftMap` 字段正确。

### Task 5: `remap_line_range` 纯函数（edit_resolve.py）

- [ ] 5.1 新增 `remap_line_range(start, end, span_steps) -> tuple[int,int] | None`
- [ ] 5.2 初始 pending = `[(start, end)]`；对每个 step：对每个 pending range 调用 `_remap_old_range`（从 file_state 导入），收集所有 remapped ranges 作为下一轮 pending
- [ ] 5.3 任一步后 pending 为空 → `None`；最终 pending 数量 ≠ 1 → `None`；否则返回唯一 range 的 `(start_line, end_line)`
- [ ] 5.4 注意：`_remap_old_range` 返回 `list[dict]`，需提取 `start_line`/`end_line`

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_coverage_fingerprint.py -k "remap_line_range" -v` — 覆盖：(a) 无 step 返回原范围；(b) 单次 edit 偏移正确；(c) 多次 edit 累积偏移；(d) range 完全落入删除区 → `None`；(e) range 被拆分成多段 → `None`；(f) **等价性不变式**：对同一 read epoch range，`remap_line_range` 结果与 coverage ranges 在同系列 edits 后的坐标系一致。

### Task 6: `DriftFallbackResult` 与回退函数（edit_execute.py）

- [ ] 6.1 新增 `class DriftFallbackResult(NamedTuple)`：`match`、`error`、`matched_map`、`remapped_range`（按设计 :184-189）
- [ ] 6.2 新增 `_find_text_segment_with_drift_fallback(lines, start_no, end_no, prefix, suffix, maps) -> DriftFallbackResult`，按设计 :191-203 逻辑实现
- [ ] 6.3 按 epoch 从新到旧遍历 maps；`remap_line_range` 返回 `None` 或与原行号相同则跳过；收集成功候选
- [ ] 6.4 0 候选 → 返回第一次错误；1 候选 → 返回命中；多候选同 range → 等价命中；多候选不同 range → ambiguity 错误

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_edit.py -k "drift_fallback" -v` — 直接构造 `lines` + `maps`，验证各分支返回。

### Task 7: 接入 `_execute_text_replace`（edit_execute.py）

- [ ] 7.1 在 :128 替换：先 `maps = get_line_drift_maps(ctx, path)`，再调用 `_find_text_segment_with_drift_fallback`
- [ ] 7.2 处理返回：`result.match is None` 时返回 `result.error`；否则用 `result.match` 的 `start_line`/`end_line` 继续 coverage 检查与切片
- [ ] 7.3 回退命中（`result.matched_map is not None`）时，在最终 `ToolResult.output` 追加提示：`[Line drift fallback: {file_path} epoch #{epoch} start_no {old}→{new}, end_no {old}→{new} matched via drift map.]`
- [ ] 7.4 `old_ranges` 提取逻辑（:140-143）不变

**测试**：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_edit.py -k "drift_fallback_e2e" -v` — 端到端：read → edit 产生漂移 → 用老行号 replace 触发回退 → 验证编辑成功且 output 含 fallback 提示。

### Task 8: 回归测试

- [ ] 8.1 运行 edit 相关全量测试：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_edit.py tests/test_tools/test_file_ops_coverage_fingerprint.py -v`
- [ ] 8.2 运行 file_ops 目录相关测试：`.venv/bin/python -m pytest tests/test_tools/test_file_ops_read.py tests/test_tools/test_file_ops_read_write.py tests/test_tools/test_file_ops_write_file.py -v`
- [ ] 8.3 确认无回归：所有测试 GREEN

## Risks

- **等价性不变式破坏**：`remap_line_range` 必须严格复用 `_remap_old_range` 语义，否则回退命中但 coverage 失败。Task 5 测试 (f) 覆盖此不变式。
- **覆盖式写入遗漏**：`record_read_range` / `remap_read_coverage_from_file_diff` 原为整体重建 dict，改造时若漏掉 `line_drift_maps` 字段会导致状态丢失。Task 2/3 测试验证保留行为。
- **多候选歧义**：多个 epoch map 同时命中不同 range 时必须返回 ambiguity 错误，不能误改。Task 6 测试覆盖。
- **FIFO 淘汰与 epoch 复用**：epoch 为 per-file 单调递增，淘汰后不复用。Task 2.2 + 测试验证。
- **compaction 清空**：`compaction.py` 直接 `clear()` 整个 `file_read_coverage`，drift maps 不跨 compaction 存活——符合设计，无需特殊处理，但实现不应依赖跨 compaction 假设。
