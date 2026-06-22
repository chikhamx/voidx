# line 工具改造：append 操作 + insert 0-based + file create hint 更新

## Context

当前 `line` 工具存在两个体验问题：

1. **追加内容需要 `lineno=-1`** — LLM 必须记住 `-1` 表示文件末尾，这是一个不自然的约定。实际使用中，`file create` 后追加内容是最常见的场景，应该有更直观的操作。
2. **insert 的 lineno 语义混乱** — 当前 `lineno` 对 insert 表示"插入到这行之后"，是 1-based 但 0 表示文件开头、-1 表示末尾，描述需要大量额外说明。

## Goals and Non-Goals

### Goals

- 新增 `op="append"` 操作，直接追加到文件末尾，无需 lineno 参数
- `op="insert"` 的 `lineno` 改为 0-based，表示"插入到这行之前"，描述无需额外说明
- `file create` 返回的 hint 改为使用 `line append`
- 更新所有引用 `lineno=-1` 的代码和 hint

### Non-Goals

- 不改动 `op="delete"` 的行号体系（delete 保持 1-based）
- 不改动 `replace` 工具
- 不改动 `edit` / `write` / `insert`（内部工具）的接口
- 不重新设计整个编辑工具分层

## 改动详情

### 1. 新增 `op="append"`

**LineInput 变更：**

```python
class LineInput(BaseModel):
    file_path: str = Field(description="Path to the file")
    op: Literal["insert", "append", "delete"] = Field(
        description=(
            "Line operation: insert content before a line, "
            "append content to end of file, or delete lines."
        )
    )
    lineno: int | None = Field(
        default=None,
        description=(
            "For insert: 0-based line number to insert before. "
            "For delete: 1-based first line to delete."
        )
    )
    end_no: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For delete only: last line to delete (1-based). "
            "If omitted, deletes only the lineno line."
        )
    )
    new_string: str = Field(
        default="",
        description=(
            "For insert/append: content to add. A trailing newline does not add "
            "an extra blank line."
        )
    )
```

**append 行为：**

- 不需要 `lineno` 参数（传入则忽略）
- 追加到文件末尾，等价于旧版 `op="insert", lineno=-1`
- 空文件也可 append（无需 read coverage，因为不涉及已有行）
- 非空文件 append 不需要 read coverage（不修改任何已有行）
- `new_string` 为空时返回 "No changes"

**append 实现逻辑：**

```python
async def _execute_line_append(ctx: ToolContext, inp: LineInput) -> ToolResult:
    if inp.new_string == "":
        return ToolResult(title="No changes", ...)

    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: ...", metadata={"error": True})
    if not path.exists():
        return ToolResult(output=f"File not found: ...", metadata={"error": True})

    original = path.read_text(encoding="utf-8", errors="replace")
    total_lines = len(_split_display_lines(original).lines)

    # append = insert after last line, no read coverage needed
    return await _apply_single_line_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", total_lines, total_lines, inp.new_string),
    )
```

### 2. insert lineno 改为 0-based

**语义变更：**

| 旧版 (1-based, insert after) | 新版 (0-based, insert before) | 含义 |
|------|------|------|
| `lineno=0` | `lineno=0` | 文件开头（语义不变） |
| `lineno=1` | `lineno=1` | 第 1 行之前（旧版：第 1 行之后） |
| `lineno=2` | `lineno=2` | 第 2 行之前（旧版：第 2 行之后） |
| `lineno=-1` | 不再使用，改用 `append` | 文件末尾 |

**关键变化：**

- `lineno` 从"插入到这行之后"变为"插入到这行之前"
- 0-based：`lineno=0` 表示在第 1 行之前插入（文件开头）
- 删除 `lineno=-1` 的特殊值
- `lineno` 的 `ge` 约束从 `-1` 改为 `0`
- 描述简化为 `"0-based line number to insert before."`，无需额外说明

**insert 实现逻辑变更：**

```python
async def _execute_line_insert(ctx: ToolContext, inp: LineInput) -> ToolResult:
    if inp.new_string == "":
        return ToolResult(title="No changes", ...)

    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: ...", metadata={"error": True})
    if not path.exists():
        return ToolResult(output=f"File not found: ...", metadata={"error": True})

    original = path.read_text(encoding="utf-8", errors="replace")
    total_lines = len(_split_display_lines(original).lines)

    # 0-based insert before: lineno=N means insert before line (N+1) in 1-based
    # ResolvedEdit("insert", X, X, ...) inserts after line X (1-based)
    # So: insert before line (N+1) = insert after line N = ResolvedEdit("insert", N, N, ...)
    # lineno=0 → insert before line 1 → ResolvedEdit("insert", 0, 0, ...) (BOF prepend)
    resolved_lineno = inp.lineno
    if inp.lineno > total_lines:
        return ToolResult(
            output=f"Cannot insert before line {inp.lineno + 1}: file has {total_lines} lines.",
            metadata={"error": True},
        )

    # Read coverage check: inserting before line (lineno+1), need coverage for that line
    if total_lines > 0 and inp.lineno <= total_lines:
        coverage_error = check_read_coverage(ctx, path, inp.lineno + 1, inp.lineno + 1)
        if coverage_error:
            return ToolResult(output=coverage_error, metadata={"error": True})

    return await _apply_single_line_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", resolved_lineno, resolved_lineno, inp.new_string),
    )
```

**注意：** `ResolvedEdit` 内部的 `start_line` 仍然是 1-based 的 "insert after line X" 语义。0-based 的 `lineno=N` 表示"在第 N+1 行之前" = "在第 N 行之后"，所以 `resolved_lineno = inp.lineno` 恰好直接对应 `ResolvedEdit.start_line`。

### 3. file create hint 更新

**当前 hint：**

```python
hint = (
    f"Use the line tool to append content to {inp.file_path} in batches of up to 30 lines. "
    f"Start with line(file_path=\"{inp.file_path}\", op=\"insert\", lineno=-1, new_string=\"...\")."
)
```

**新 hint：**

```python
hint = (
    f"Use the line tool to append content to {inp.file_path} in batches of up to 30 lines. "
    f"Start with line(file_path=\"{inp.file_path}\", op=\"append\", new_string=\"...\")."
)
```

## 受影响文件

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/file_ops/line.py` | LineInput 模型、LineTool.execute、_execute_line_insert、新增 _execute_line_append |
| `src/voidx/tools/file_ops/file.py` | _create_file 的 next_step_hint |
| `src/voidx/tools/file_ops/edit_execute.py` | FileInsertTool 的 lineno==-1 逻辑 |
| `src/voidx/tools/bash_router.py` | 4 处 llm_hint 从 `op="insert", lineno=-1` 改为 `op="append"`；2 处 `lineno=0` 改为 `op="append"` |
| `src/voidx/llm/compaction.py` | compaction prune 逻辑增加 `op="append"` 分支 |
| `src/voidx/permission/rules.py` | repair_tool_name 中 `"insert": "line"` 无需改动（insert 仍是 line 工具的操作） |
| `tests/test_tools/test_file_ops_line_file.py` | 更新 lineno=-1 相关测试，新增 append 测试 |
| `tests/test_tools/test_file_ops_edit.py` | 更新 insert lineno 测试 |
| `tests/test_tools/test_file_ops_coverage_fingerprint.py` | 更新 insert lineno 测试 |
| `tests/test_llm/test_prune_args.py` | 新增 append compaction 测试 |

## 边界情况

| 场景 | 行为 |
|------|------|
| `op="append"` + 空文件 | 正常追加，无需 read coverage |
| `op="append"` + 非空文件 | 追加到末尾，无需 read coverage |
| `op="append"` + `new_string=""` | 返回 "No changes" |
| `op="append"` + 传入 `lineno` | 忽略 lineno |
| `op="insert"` + `lineno=0` | 在文件开头插入（与旧版行为一致） |
| `op="insert"` + `lineno` 超出文件行数 | 报错 |
| `op="delete"` + `lineno` | 保持 1-based，不变 |
| `file create` overwrite | 不返回 append hint（与旧版一致） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| append 不需要 read coverage | append 也要求 read coverage | append 不修改任何已有行，强制 read 是不必要的摩擦 |
| insert 改为 0-based insert-before | 保持 1-based insert-after | 0-based 与 read 输出行号对齐（read 输出 1-based，0-based lineno=N 表示在第 N+1 行前插入），描述更简洁 |
| append 忽略 lineno | append 传入 lineno 报错 | 忽略更宽容，减少 LLM 出错概率 |
| delete 保持 1-based | delete 也改为 0-based | delete 的 lineno 语义是"删除这行"，1-based 更自然，且改动范围最小化 |
