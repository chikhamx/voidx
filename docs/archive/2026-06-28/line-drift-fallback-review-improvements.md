> **Status: Done**

# 行号漂移回退匹配 — Review 改进记录

> 依据设计文档：`docs/archive/2026-06-28/line-drift-fallback.md`
> 依据实现计划：`docs/archive/2026-06-28/line-drift-fallback-plan.md`

## Context

`line-drift-fallback` 功能已实现并通过 review（PASS）。review 提出 3 个非阻塞建议，用户确认全部落实。本文档记录这 3 项改进的实现过程与结果。

### 改进项

| # | 建议 | 类型 | 价值 |
|---|------|------|------|
| 1 | `remap_line_range` 的 `span_steps` 类型标注过于宽松（`list[list]`） | 类型标注微调 | 提升类型检查可读性，明确 step 元素是 `DiffSpan` |
| 2 | E2E 测试只覆盖单次 edit 漂移，缺多次 edit 的 step 累积集成测试 | 测试补强 | 抓住 step append 顺序与坐标系衔接的集成问题 |
| 3 | `MAX_LINE_DRIFT_MAPS_PER_FILE` 的 FIFO 淘汰逻辑已有测试但被重复定义 | 去重 | 删除重复的 `test_fifo_eviction_when_exceeding_max`，保留原有（第 377 行） |

## Goals and Non-Goals

### Goals

- 落实 review 的 3 项建议，代码与测试均已存在
- 改进不改变功能行为，仅提升类型准确性和测试覆盖

### Non-Goals

- 不修改 drift fallback 的核心逻辑（`_find_text_segment_with_drift_fallback`、`remap_line_range` 的算法）
- 不调整 `MAX_LINE_DRIFT_MAPS_PER_FILE` 的值（维持 16）
- 不改变设计文档中的 Decisions Log

## Architecture

三项改进相互独立，分别落在不同文件：

```
改进 #1 (类型标注)
  └─ src/voidx/tools/file_ops/edit_resolve.py
       ├─ 新增 `from typing import TYPE_CHECKING`
       ├─ 新增 `if TYPE_CHECKING: from voidx.tools.file_state import DiffSpan`
       └─ `remap_line_range` 的 span_steps: list[list] → "list[list[DiffSpan]]"

改进 #2 (E2E 多次 edit 累积测试)
  └─ tests/test_tools/test_file_ops_edit.py
       └─ TestDriftFallbackE2E.test_drift_fallback_accumulates_multiple_edits

改进 #3 (FIFO 淘汰重复测试去重)
  └─ tests/test_tools/test_file_ops_coverage_fingerprint.py
       └─ 删除 TestGetLineDriftMaps.test_fifo_eviction_when_exceeding_max（重复）
       └─ 保留 TestLineDriftMapLifecycle.test_fifo_eviction_when_exceeding_max（第 377 行）
```

## Data Model

无数据模型变更。`LineDriftMap`、`DiffSpan`、`ReadLineRange` 结构不变。

## API Contract

### 改进 #1：`remap_line_range` 类型标注

- **文件**：`src/voidx/tools/file_ops/edit_resolve.py:240`
- **变更前**：`span_steps: list[list]`
- **变更后**：`span_steps: "list[list[DiffSpan]]"`
- **循环依赖处理**：`DiffSpan` 定义在 `voidx.tools.file_state`，而 `edit_resolve.py` 通过函数内延迟导入 `from voidx.tools.file_state import _remap_old_range` 调用其内部函数。为避免模块级循环导入，使用 `TYPE_CHECKING` 守卫导入 `DiffSpan`，配合 `from __future__ import annotations`（PEP 563）使标注在运行时惰性求值，类型检查器（mypy/pyright）可见。

### 改进 #2：E2E 多次 edit 累积测试

- **文件**：`tests/test_tools/test_file_ops_edit.py`，`TestDriftFallbackE2E` 类
- **测试名**：`test_drift_fallback_accumulates_multiple_edits`
- **场景**：
  ```
  read 1-10
  edit1: l2-l4 -> X (3行->1行,偏移 -2),l10 从第 10 行 -> 第 8 行
  edit2: l5-l7 -> Y (3行->1行,偏移 -2),l10 从第 8 行 -> 第 6 行
  LLM 用老行号 10-10 找 "l10":
    首次 10±3=7-13 搜索,文件只有 6 行 -> 失败
    回退: remap 10 -> 8 (edit1) -> 6 (edit2),重试匹配 l10 -> 成功
  ```
- **断言**：
  - `result.metadata.get("error") is not True`
  - `"drift fallback" in result.output.lower()`
  - `"epoch #1" in result.output`（验证命中的是最初 read 的 epoch）
  - `f.read_text() == "l1\nX\nY\nl8\nl9\nL10\n"`

### 改进 #3：FIFO 淘汰重复测试去重

- **文件**：`tests/test_tools/test_file_ops_coverage_fingerprint.py`
- **情况**：review 时误判 FIFO 淘汰无测试覆盖，实际已有 `test_fifo_eviction_when_exceeding_max`（第 377 行，`TestLineDriftMapLifecycle` 类）。改进过程中又新增了一个同名测试（`TestGetLineDriftMaps` 类），造成重复定义。
- **处理**：删除新增的重复测试，保留原有的（断言 `maps[0].epoch == 2` 和 `maps[-1].epoch == MAX+1`）。
- **原有测试断言**：
  - `len(maps) == MAX_LINE_DRIFT_MAPS_PER_FILE`（16）
  - `maps[0].epoch == 2`（epoch 1 被淘汰）
  - `maps[-1].epoch == MAX_LINE_DRIFT_MAPS_PER_FILE + 1`

## Error Handling

无新增错误场景。三项改进不改变运行时错误处理路径。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| #1 用 `TYPE_CHECKING` 守卫导入 `DiffSpan` | 在模块级直接导入 `DiffSpan` | `edit_resolve.py` 已通过函数内延迟导入 `file_state._remap_old_range` 避免循环依赖；模块级直接导入 `DiffSpan` 会引入循环。`TYPE_CHECKING` 守卫 + PEP 563 惰性标注兼顾类型可见性与无循环导入 |
| #2 测试场景用偏移 -2 × 2 = -4（而非更大偏移） | 用单次大偏移（如 -6） | 两次独立 edit 才能验证 step 序列的 append 顺序和坐标系衔接——单次大偏移只能验证单 step，与现有 `test_drift_fallback_e2e` 重复 |
| #3 删除重复测试，保留原有 | 保留新增的（断言更完整） | 原有测试（第 377 行）已充分覆盖 FIFO 淘汰的边界条件；重复定义会被 pytest 忽略且造成维护混乱。review 时应先 grep 确认是否已有测试，避免误判 |

## Verification

### 测试命令与结果

```
$ .venv/bin/python -m pytest tests/test_tools/test_file_ops_edit.py tests/test_tools/test_file_ops_coverage_fingerprint.py -q
110 passed in 1.55s
```

新增 1 个测试、删除 1 个重复测试（原 109 + 1 - 0 = 110，重复测试被 pytest 忽略故原计数不变）：
- `TestDriftFallbackE2E::test_drift_fallback_accumulates_multiple_edits` — PASSED（新增）
- `TestGetLineDriftMaps::test_fifo_eviction_when_exceeding_max` — DELETED（与 `TestLineDriftMapLifecycle` 同名重复）

### 回归验证

```
$ .venv/bin/python -m pytest tests/test_tools/ -q
606 passed in 9.45s
```

tools 套件全绿，无回归。

## Files Changed

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/file_ops/edit_resolve.py` | 新增 `TYPE_CHECKING` 导入与 `DiffSpan` 守卫导入；`remap_line_range` 的 `span_steps` 类型标注改为 `"list[list[DiffSpan]]"` |
| `tests/test_tools/test_file_ops_edit.py` | 新增 `test_drift_fallback_accumulates_multiple_edits` E2E 测试 |
| `tests/test_tools/test_file_ops_coverage_fingerprint.py` | 删除重复的 `test_fifo_eviction_when_exceeding_max`（保留 `TestLineDriftMapLifecycle` 中的原有定义） |
