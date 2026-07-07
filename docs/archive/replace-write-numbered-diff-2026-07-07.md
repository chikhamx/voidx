> **Status: Done**

# Replace/Write 输出带行号 — 技术设计文档

## Context

`replace` 和 `write` 工具返回的 diff 内容目前是标准 unified diff 格式，每一行没有行号前缀。LLM 只能从 hunk header（`@@ -10,7 +10,7 @@`）推断行号，无法像 `read` 输出那样每行直接看到 `{lineno}\t{content}`。

用户希望在 LLM 可见的 tool output 中，每一行也带上行号，降低 LLM 推算行号的认知负担。

## Goals and Non-Goals

### Goals

- `replace` 工具返回的每行 diff 内容带上行号前缀
- `write` 工具返回的每行 diff 内容带上行号前缀
- 格式与 `read` 一致：`{lineno}\t{content}`
- `result.diff` 字段保持标准 unified diff 不变

### Non-Goals

- 不改 UI 侧 diff 渲染（`Syntax(diff, "diff")` 保持原样）
- 不改 `diff_stat()`、`parse_unified_diff()` 等依赖标准 diff 格式的解析逻辑
- 不改 `read` 工具的格式
- 不改 `file` 工具的 create/overwrite/delete 输出。`file.py` 的 create/overwrite（约第 91 行）和 delete（约第 126 行）虽然也调用 `make_file_diff` 并将结果存入 `result.diff`，但其 `result.output` 本身不拼接 diff 文本，LLM 不会在 output 中看到 diff，因此不在本次改动范围。后续如需覆盖，需先解决 `make_structured_diff` 不支持 `old_label`/`new_label` 的问题（见 Error Handling）。

## Architecture

**约束**：`result.diff` 字段被多处 UI 逻辑使用，必须保持标准 unified diff 格式。只能修改 `result.output`（即 `ToolMessage.content`，LLM 直接看到的文本）。

**方案**：新增一个渲染函数 `_render_numbered_diff(file_diff: FileDiff) -> str`，复用已有 `make_structured_diff()` 产生的结构化 `FileDiff` 数据（已包含每行的 `kind` / `old_lineno` / `new_lineno`），将其渲染为带行号的文本。`edit_execute.py` 中原来用 `make_file_diff()` 拼接 output 的地方，改为用 `_render_numbered_diff(file_diff)`。

```
edit_execute.py                          diffing.py
┌─────────────────────┐                 ┌──────────────────┐
│ make_file_diff()    │──→ result.diff  │ unified_diff     │
│ make_structured_diff│──→ file_diff    │ FileDiff         │
│ _render_numbered_diff(file_diff) │──→ output text (LLM可见) │
└─────────────────────┘                 └──────────────────┘
```

### 数据流

1. `_execute_text_replace` / `_apply_resolved_edits` 读取原始文件内容 → `original`
2. 执行替换/插入操作 → `new_content`
3. `make_file_diff(file_path, original, new_content)` → 标准 unified diff → 存入 `result.diff`
4. `make_structured_diff(file_path, original, new_content)` → `FileDiff` → 用于覆盖映射更新
5. 新：`_render_numbered_diff(file_diff)` → 带行号的文本 → 拼入 `result.output`

## API Contract

### `_render_numbered_diff(file_diff: FileDiff) -> str`

**功能**：将结构化 FileDiff 渲染为带行号的文本，格式类似 unified diff 但每行内容前缀为 `{diff_marker}{lineno}\t{text}`。

**输入**：`FileDiff` 对象（来自 `make_structured_diff`）

**输出格式**：

```
--- a/{filepath}
+++ b/{filepath}
@@ -{old_start},{old_count} +{new_start},{new_count} @@ {section}
-{old_lineno}\t{text}       # remove 行
+{new_lineno}\t{text}       # add 行
 {new_lineno}\t{text}       # context 行
```

**行号规则**：
| DiffLine.kind | line_no 来源 | 前缀 |
|--------------|-------------|------|
| `remove`     | `old_lineno` | `-`  |
| `add`        | `new_lineno` | `+`  |
| `context`    | `new_lineno` | ` `  |

### 调用点

#### `edit_execute.py:_execute_text_replace`（约第 376-396 行）

当前：
```python
diff = make_file_diff(file_path, original, content)
file_diff = make_structured_diff(file_path, original, content)
remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

output = f"File edited: {file_path} (1 operations)"
if drift_hint:
    output = f"{drift_hint}{output}"
if diff:
    output = f"{output}\n{diff}"
return ToolResult(
    title="Edited (1 edits)",
    output=output,
    summary="Edited (1 operations)",
    metadata={
        "file": file_path,
        "operations": 1,
        "start_line": actual_start_line,
        "end_line": actual_end_line,
    },
    diff=diff,
)
```

改为：
```python
diff = make_file_diff(file_path, original, content)
file_diff = make_structured_diff(file_path, original, content)
remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

numbered_diff = _render_numbered_diff(file_diff)
output = f"File edited: {file_path} (1 operations)"
if drift_hint:
    output = f"{drift_hint}{output}"
if numbered_diff:
    output = f"{output}\n{numbered_diff}"
return ToolResult(
    title="Edited (1 edits)",
    output=output,
    summary="Edited (1 operations)",
    metadata={
        "file": file_path,
        "operations": 1,
        "start_line": actual_start_line,
        "end_line": actual_end_line,
    },
    diff=diff,
)
```

#### `edit_execute.py:_apply_resolved_edits`（约第 473-488 行）

当前：
```python
diff = make_file_diff(file_path, original, content)
file_diff = make_structured_diff(file_path, original, content)
remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

details = "\n".join([*hints, *_line_shift_hints(edits), diff])
output = f"File edited: {file_path} ({len(edits)} operations)"
if details:
    output = f"{output}\n{details}"

return ToolResult(
    title=f"Edited ({len(edits)} edits)",
    output=output,
    summary=f"Edited ({len(edits)} operations)",
    metadata={"file": file_path, "operations": len(edits)},
    diff=diff,
)
```

改为：
```python
diff = make_file_diff(file_path, original, content)
file_diff = make_structured_diff(file_path, original, content)
remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

numbered_diff = _render_numbered_diff(file_diff)
details = "\n".join([*hints, *_line_shift_hints(edits), numbered_diff])
output = f"File edited: {file_path} ({len(edits)} operations)"
if details:
    output = f"{output}\n{details}"

return ToolResult(
    title=f"Edited ({len(edits)} edits)",
    output=output,
    summary=f"Edited ({len(edits)} operations)",
    metadata={"file": file_path, "operations": len(edits)},
    diff=diff,
)
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `file_diff.hunks` 为空 | 返回空字符串，output 中省略 diff 部分（和当前 `make_file_diff` 返回空时行为一致） |
| `FileDiff` 为新建/删除文件（无旧内容） | 正常渲染，`old_start=0` 或 `new_start=0` 时按标准 unified diff 处理 |
| `make_structured_diff` 不支持 `old_label`/`new_label` | `make_structured_diff` 硬编码 `a/{filepath}` 和 `b/{filepath}`，而 `file.py` 的 delete 路径用 `make_file_diff(..., new_label="/dev/null")`。本次不改 `file.py`，不触发此问题；后续如需覆盖 delete 场景，需先给 `make_structured_diff` 加 label 参数或单独处理 `/dev/null` 约定 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 新增 `_render_numbered_diff`，而非修改 `make_file_diff` | 直接改 `make_file_diff` 加参数 | `result.diff` 需要保持标准格式，改 `make_file_diff` 会同时影响两处；新函数职责单一 |
| 复用 `make_structured_diff` 的输出 | 在 `_render_numbered_diff` 内自己跑 `SequenceMatcher` | `make_structured_diff` 已在两个调用点被调用，数据已有，避免重复计算 |
| 行号格式使用 tab 分隔 `\t` | 空格对齐、括号标示等 | 与 `read` 的 `{line_number}\t{line}` 格式一致，LLM 已熟悉此格式 |

## Open Questions

无。`_render_numbered_diff` 先用模块级私有函数（下划线前缀），后续如有其他调用方再公开。

## Test Strategy

### 单元测试（`test_diffing.py` 或新建 `test_render_numbered_diff.py`）

1. **context 行**：构造一个仅含 context 行的 `FileDiff`，验证输出中行号取自 `new_lineno`，前缀为空格。
2. **add 行**：验证前缀为 `+`，行号取自 `new_lineno`。
3. **remove 行**：验证前缀为 `-`，行号取自 `old_lineno`。
4. **空 hunks**：`make_structured_diff(path, old, old)` 返回的 `FileDiff` 无 hunks，`_render_numbered_diff` 应返回空字符串。
5. **新建文件**：`make_structured_diff(path, "", new)` 的 `operation="Create"`，验证 `old_start=0` 时渲染正常。
6. **删除文件**：`make_structured_diff(path, old, "")` 的 `operation="Delete"`，验证 `new_start=0` 时渲染正常。

### Round-trip 一致性测试

验证 `_render_numbered_diff(make_structured_diff(path, old, new))` 输出中的行号与 `parse_unified_diff(make_file_diff(path, old, new))` 解析出的 `old_lineno`/`new_lineno` 一致。确保两种 diff 生成路径的行号语义不会漂移。

### 集成测试（`test_edit_execute.py`）

调用 `replace` 工具后，验证 `result.output` 中包含带行号的 diff 文本，且 `result.diff` 仍为标准 unified diff 格式（可被 `parse_unified_diff` 解析）。
