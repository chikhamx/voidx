> **Status: Done**

# Replace 自动创建文件 — 技术设计文档

## Context

LLM 经常对不存在的文件直接调用 `replace` 工具（而非先 `file create` 再 `replace`），当前 `_resolve_edit_target` 在文件不存在时直接返回 `File not found` 错误，LLM 需要重试。用户希望：文件不存在时，直接用 `new_string` 创建文件，创建失败才报错。

## Goals and Non-Goals

### Goals

- `replace` 工具在目标文件不存在时，直接用 `new_string` 内容创建文件
- 创建成功返回 `File created` 结果（非错误），LLM 无需重试
- 路径安全检查（`resolve_safe`）仍然生效
- 文件状态正确更新（`record_mtime`、`clear_read_coverage`）
- diff 输出正确（`original=""` → `content=new_string`，即纯新增）

### Non-Goals

- 不改 `write` 工具的 insert/append 路径（它们也有 file-not-found 报错，但用户只提了 replace）
- 不改 `file` 工具的 create 路径
- 不改 `_resolve_edit_target` 函数本身（它在 `write` 的 append 路径也被调用，改它会影响 write）

## Architecture

**约束**：`_resolve_edit_target` 被 `replace`（`_execute_text_replace`）和 `write`（`_apply_single_write_edit`）共用。直接改 `_resolve_edit_target` 会影响 write 的 append 路径。因此拦截点放在 `_execute_text_replace` 内部，在 `_resolve_edit_target` 返回 file-not-found 错误时，走自动创建分支。

**方案**：在 `_execute_text_replace` 中，当 `_resolve_edit_target` 返回的 error output 以 `File not found:` 开头时，不直接返回错误，而是：

1. 重新 `resolve_safe` 拿到 path（沙箱检查）
2. `path.parent.mkdir(parents=True, exist_ok=True)` 创建父目录
3. `path.write_text(new_string, encoding="utf-8")` 写入内容
4. `record_mtime(ctx, path)` + `clear_read_coverage(ctx, path)` 更新状态
5. 生成 diff（`make_file_diff` + `make_structured_diff`，`original=""`）
6. 返回 `ToolResult(title="File created", output="File created: {file_path}\n{numbered_diff}", ...)`

```
_execute_text_replace
├── _resolve_edit_target(ctx, file_path)
│   ├── path traversal blocked → return error (不变)
│   ├── file not found → 走自动创建分支 ← 新增
│   │   ├── resolve_safe 拿 path
│   │   ├── mkdir parent + write_text(new_string)
│   │   ├── record_mtime + clear_read_coverage
│   │   ├── make_file_diff + make_structured_diff (original="")
│   │   └── return ToolResult(title="File created")
│   ├── stale → return error (不变)
│   └── ok → 正常 replace 流程 (不变)
```

### 数据流

1. `_resolve_edit_target` → 返回 `(None, error)` 且 error.output 以 `File not found:` 开头
2. `resolve_safe(ctx.workspace, file_path, ...)` → `path`（沙箱检查通过）
3. `path.parent.mkdir(parents=True, exist_ok=True)` → 创建父目录
4. `path.write_text(new_string)` → 写入内容
5. `record_mtime(ctx, path)` + `clear_read_coverage(ctx, path)` → 更新文件状态
6. `make_file_diff(file_path, "", new_string)` → 标准 diff → `result.diff`
7. `make_structured_diff(file_path, "", new_string)` → `FileDiff` → `_render_numbered_diff` → `result.output`
8. 返回 `ToolResult(title="File created", ...)`

## API Contract

### `_execute_text_replace` 自动创建分支

**触发条件**：`_resolve_edit_target` 返回 error，且 `error.output` 以 `"File not found:"` 开头

**行为**：
- 用 `new_string` 创建文件
- 返回非错误结果

**返回 `ToolResult`**：
```python
ToolResult(
    title="File created",
    output=f"File created: {file_path}\n{numbered_diff}",
    summary="File created",
    metadata={
        "file": file_path,
        "operations": 1,
        "auto_created": True,
    },
    diff=diff,
)
```

**异常处理**：如果 `path.write_text` 抛异常（权限不足、磁盘满等），捕获并返回错误：
```python
ToolResult(
    output=f"Failed to create file: {file_path}\n{exc}",
    metadata={"error": True},
)
```

### `FileReplaceTool.description` 更新

在 description 末尾追加：
```
If the file does not exist, it is created with new_string as its content.
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 路径遍历攻击 | `resolve_safe` 返回 None，返回 `Path traversal blocked` 错误（不变） |
| 父目录创建失败 | `mkdir` 抛异常，捕获返回 `Failed to create file` 错误 |
| 文件写入失败 | `write_text` 抛异常，捕获返回 `Failed to create file` 错误 |
| 文件已存在 | 不触发自动创建分支，走正常 replace 流程（不变） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 拦截点放在 `_execute_text_replace` 而非 `_resolve_edit_target` | 改 `_resolve_edit_target` | `_resolve_edit_target` 被 write 的 append 路径共用，改它会影响 write。放 `_execute_text_replace` 只影响 replace |
| 用 `new_string` 直接创建而非创建空文件 | 创建空文件后继续走 replace 逻辑 | 空文件没有行可匹配，replace 仍会失败，LLM 需重试。直接用 new_string 创建才真正解决问题 |
| 判断条件用 `error.output.startswith("File not found:")` | 给 `_resolve_edit_target` 加返回码或 error_kind | 改动最小，且当前 error output 格式稳定 |
| 不改 write 的 insert/append 路径 | 同时改 write | 用户只提了 replace；write 的 insert 有 lineno 语义，自动创建时 lineno 无意义，需要单独设计 |

## Open Questions

无。
