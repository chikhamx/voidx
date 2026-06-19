# 拆分 tools/file_ops.py — 技术设计文档

## Context

`src/voidx/tools/file_ops.py` 当前 1030 行，包含 5 个 Tool 类（Read/Write/Edit/Insert/Replace）、4 个共享类型、4 个常量、7 个 read 辅助函数、5 个编辑执行函数、12 个编辑解析纯函数。文件内职责混杂：read 输出格式化、write 逻辑、edit 定位算法、edit 执行编排全部平铺在一个文件中。

`tools/` 目录已有 25 个 .py 文件，均为单工具或单职责模块。`file_ops.py` 是唯一超过 1000 行的文件，且内部 5 个工具的代码可按职责清晰分组。

## Goals and Non-Goals

### Goals

- 将 `file_ops.py` 拆分为 `file_ops/` 子包，5 个模块各 < 350 行
- 通过 `__init__.py` re-export 保持所有公开 API 导入路径不变
- 不改变任何运行时行为，纯结构重构

### Non-Goals

- 不改变 Tool 类的接口或参数 schema
- 不合并或重构 edit/insert/replace 的执行逻辑
- 不拆分 `file_state.py`（独立的读覆盖/版本管理模块）

## Architecture

### 新文件结构

```
src/voidx/tools/
├── file_ops/                  # 子包（原 file_ops.py 拆分而来）
│   ├── __init__.py            # re-export 公开 API（~20 行）
│   ├── types.py               # 共享类型 + 常量（~60 行）
│   ├── read.py                # FileReadTool + read 辅助函数（~220 行）
│   ├── write.py               # FileWriteTool（~80 行）
│   ├── edit_resolve.py        # 编辑定位/解析纯函数（~310 行）
│   └── edit_execute.py        # 3 个编辑工具 + 执行函数（~360 行）
└── file_state.py              # 不变
```

删除：`src/voidx/tools/file_ops.py`

### 模块职责

#### `types.py` — 共享类型与常量（~60 行）

多个子模块依赖 `ResolvedEdit`、`DisplayLines` 等类型，提取到独立文件消除循环依赖。

| 符号 | 来源行 | 迁移原因 |
|------|--------|----------|
| `READ_OUTPUT_MAX_CHARS` | L27 | read.py 引用 |
| `BINARY_DETECTION_BYTES` | L28 | read.py 引用 |
| `TEXT_REPLACE_LINE_RADIUS` | L29 | edit_resolve.py 引用 |
| `TEXT_REPLACE_SPAN_TOLERANCE` | L30 | edit_resolve.py 引用 |
| `DisplayLines` | L33 | read.py、edit_execute.py 引用 |
| `ResolvedEdit` | L38 | edit_resolve.py、edit_execute.py 引用 |
| `ParagraphResolution` | L45 | edit_resolve.py 定义，edit_execute.py 引用 |
| `BoundedReadOutput` | L50 | read.py 定义和引用 |

#### `read.py` — Read 工具 + 输出格式化（~220 行）

| 符号 | 来源行 | 行数 |
|------|--------|------|
| `_split_display_lines` | L59 | 7 |
| `_split_edit_lines` | L68 | 6 |
| `_join_display_lines` | L76 | 5 |
| `_read_continuation_note` | L83 | 5 |
| `_overlong_line_output` | L90 | 12 |
| `_numbered_output_with_note` | L103 | 4 |
| `_binary_null_byte_detected` | L109 | 3 |
| `_bounded_truncated_output` | L114 | 9 |
| `_bounded_numbered_read_output` | L125 | 53 |
| `FileReadInput` | L180 | 6 |
| `FileReadTool` | L186 | 92 |

> **说明**：`_split_edit_lines` 和 `_join_display_lines` 被 edit_execute.py 使用，但它们是通用的行分割/拼接函数，与 `_split_display_lines` 同族，放在 read.py（行处理工具集）比放在 types.py 更合理。edit_execute.py 从 `.read` 导入。

#### `write.py` — Write 工具（~80 行）

| 符号 | 来源行 | 行数 |
|------|--------|------|
| `FileWriteInput` | L279 | 10 |
| `FileWriteTool` | L289 | 63 |

#### `edit_resolve.py` — 编辑定位/解析纯函数（~310 行）

所有函数均为纯函数，无 `ctx`/`path`/IO 依赖，仅操作行列表和字符串。

| 符号 | 来源行 | 行数 |
|------|--------|------|
| `_find_paragraph` | L907 | 53 |
| `_find_text_segment` | L796 | 51 |
| `_resolve_paragraph_edits` | L896 | 9 |
| `_find_line_candidates` | L849 | 15 |
| `_rank_line_range_pairs` | L872 | 22 |
| `_line_matches_replace_anchor` | L866 | 4 |
| `_window_text` | L962 | 10 |
| `_find_snippet_matches` | L974 | 7 |
| `_global_offset_for_line` | L983 | 4 |
| `_line_for_offset` | L989 | 3 |
| `_validate_resolved_edits` | L766 | 28 |
| `_format_lines` | L994 | 3 |
| `_result_trailing_newline` | L1020 | 11 |

**依赖**：从 `.types` 导入 `ResolvedEdit`、`ParagraphResolution`、`TEXT_REPLACE_LINE_RADIUS`、`TEXT_REPLACE_SPAN_TOLERANCE`。无其他内部模块依赖。

#### `edit_execute.py` — 编辑执行 + 3 个编辑工具（~360 行）

| 符号 | 来源行 | 行数 |
|------|--------|------|
| `EditEntry` | L353 | 38 |
| `FileEditInput` | L393 | 10 |
| `FileInsertInput` | L403 | 15 |
| `FileReplaceInput` | L420 | 43 |
| `FileEditTool` | L465 | 26 |
| `FileInsertTool` | L492 | 38 |
| `FileReplaceTool` | L531 | 25 |
| `_resolve_edit_target` | L557 | 10 |
| `_execute_paragraph_edits` | L569 | 38 |
| `_execute_direct_edits` | L609 | 24 |
| `_execute_text_replace` | L635 | 62 |
| `_apply_resolved_edits` | L698 | 66 |
| `_line_shift_hints` | L998 | 20 |

**依赖**：
- 从 `.types` 导入 `ResolvedEdit`、`DisplayLines`、`ParagraphResolution`
- 从 `.read` 导入 `_split_display_lines`、`_split_edit_lines`、`_join_display_lines`
- 从 `.edit_resolve` 导入 `_resolve_paragraph_edits`、`_find_text_segment`、`_validate_resolved_edits`、`_result_trailing_newline`

> **`_line_shift_hints` 归属说明**：该函数在原文件中位于 L998（edit_resolve 区域），但它依赖 `_split_edit_lines`（read.py），且仅被 `_apply_resolved_edits`（edit_execute.py）调用。将其放入 edit_execute.py 可避免 edit_resolve → read 的跨模块依赖，保持 edit_resolve 为纯解析模块。

### `__init__.py` — Re-export 策略

```python
"""File operation tools — read, write, edit."""

from .read import FileReadInput, FileReadTool
from .write import FileWriteInput, FileWriteTool
from .edit_execute import (
    EditEntry,
    FileEditInput,
    FileEditTool,
    FileInsertInput,
    FileInsertTool,
    FileReplaceInput,
    FileReplaceTool,
)
from .edit_resolve import _find_paragraph
```

这确保以下导入路径在拆分后仍然有效：

```python
from voidx.tools.file_ops import FileReadTool, FileWriteTool, FileEditTool, FileInsertTool, FileReplaceTool
from voidx.tools.file_ops import FileReadInput, FileWriteInput, FileEditInput, FileInsertInput, FileReplaceInput
from voidx.tools.file_ops import EditEntry, _find_paragraph
```

### 导入关系

```
file_ops/types.py
  └── (无内部依赖)

file_ops/read.py
  └── from .types import DisplayLines, BoundedReadOutput, READ_OUTPUT_MAX_CHARS, BINARY_DETECTION_BYTES

file_ops/write.py
  └── from .types import DisplayLines

file_ops/edit_resolve.py
  └── from .types import ResolvedEdit, ParagraphResolution, TEXT_REPLACE_LINE_RADIUS, TEXT_REPLACE_SPAN_TOLERANCE

file_ops/edit_execute.py
  ├── from .types import ResolvedEdit, DisplayLines, ParagraphResolution
  ├── from .read import _split_display_lines, _split_edit_lines, _join_display_lines
  └── from .edit_resolve import _resolve_paragraph_edits, _find_text_segment, _validate_resolved_edits, _result_trailing_newline

file_ops/__init__.py
  └── from .read, .write, .edit_execute, .edit_resolve import ...
```

无循环依赖。`edit_execute.py` 作为顶层编排模块，依赖 `read` 和 `edit_resolve`；子模块之间无横向依赖。

## 外部导入路径变更

### `registry.py`

当前导入：
```python
from voidx.tools.file_ops import FileReadTool, FileWriteTool, FileEditTool, FileInsertTool, FileReplaceTool
```

拆分后 `file_ops` 变为包，`__init__.py` re-export 了这些符号，**此文件无需修改**。

### 测试文件

以下测试直接导入内部符号 `_find_paragraph`，拆分后通过 `__init__.py` re-export 保持路径不变，**无需修改**：

| 测试文件 | 导入符号 |
|----------|----------|
| `test_file_ops_read.py` | `_find_paragraph` |
| `test_file_ops_edit.py` | `_find_paragraph` |

其他测试文件仅导入 Tool 类和 Input 模型，均通过 `__init__.py` re-export，**无需修改**。

## 不变项

- 5 个 Tool 类的 `id`、`description`、`parameters_schema`、`execute` 签名不变
- 所有运行时行为不变
- `from voidx.tools.file_ops import FileReadTool` 等路径不变
- `file_state.py` 不变

## 验证

```bash
# 全量测试
.venv/bin/python -m pytest tests/ -v

# 导入检查 — 公开 API（路径不变）
.venv/bin/python -c "from voidx.tools.file_ops import FileReadTool, FileWriteTool, FileEditTool, FileInsertTool, FileReplaceTool; print('OK')"

# 导入检查 — Input 模型
.venv/bin/python -c "from voidx.tools.file_ops import FileReadInput, FileWriteInput, FileEditInput, FileInsertInput, FileReplaceInput, EditEntry; print('OK')"

# 导入检查 — 内部符号 re-export
.venv/bin/python -c "from voidx.tools.file_ops import _find_paragraph; print('OK')"

# 导入检查 — 子模块可导入
.venv/bin/python -c "from voidx.tools.file_ops.types import ResolvedEdit; print('types OK')"
.venv/bin/python -c "from voidx.tools.file_ops.read import FileReadTool; print('read OK')"
.venv/bin/python -c "from voidx.tools.file_ops.write import FileWriteTool; print('write OK')"
.venv/bin/python -c "from voidx.tools.file_ops.edit_resolve import _find_paragraph; print('edit_resolve OK')"
.venv/bin/python -c "from voidx.tools.file_ops.edit_execute import FileEditTool; print('edit_execute OK')"
```

## 实施顺序

1. 创建 `file_ops/` 目录和 `types.py`，迁移共享类型和常量
2. 创建 `read.py`，迁移 `FileReadTool` + read 辅助函数
3. 创建 `write.py`，迁移 `FileWriteTool`
4. 创建 `edit_resolve.py`，迁移编辑解析纯函数
5. 创建 `edit_execute.py`，迁移 3 个编辑工具 + 执行函数 + `_line_shift_hints`
6. 创建 `__init__.py`（re-export 公开 API），删除原 `file_ops.py`
7. 运行全量测试验证

每步完成后运行 `pytest tests/ -x` 确认无回归，再进行下一步。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `_split_edit_lines`/`_join_display_lines` 放 read.py | 放 types.py | 它们是行处理函数，与 `_split_display_lines` 同族；types.py 应只放数据定义 |
| `_line_shift_hints` 放 edit_execute.py | 放 edit_resolve.py | 它依赖 `_split_edit_lines`（read.py），放入 edit_resolve 会引入 edit_resolve → read 依赖，破坏 edit_resolve 的纯解析定位 |
| Input 模型随 Tool 类放 edit_execute.py | 单独放 models.py | Input 模型仅被对应 Tool 使用，4 个模型（~106 行）不足以独立成模块 |
| write.py 独立成模块 | 合并到 read.py | Write 是独立工具，与 Read 无代码共享；合并会使 read.py 过大（~300 行）且职责混杂 |
