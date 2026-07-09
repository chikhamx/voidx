> **Status: Done**

# FileDeleteTool — 技术设计文档

## Context

文件编辑工具集目前有 `read`、`write`、`edit`、`insert`、`replace`，但缺少删除能力。LLM 若要删除行，只能用 `replace` 传空字符串，语义不明确且容易误操作。需要一个意图明确的 `delete` 工具，只做单行删除，大块删除走 `replace`。

## Goals and Non-Goals

### Goals

- 支持删除文件中的单行（一次一行）
- 提供锚点校验防止删错行
- 复用现有安全机制（staleness check、read coverage）、版本保存
- 生成 diff 输出，与其他编辑工具一致

### Non-Goals

- 不支持删除整文件
- 不支持删除连续多行（走 `replace`）
- 不支持删除目录
- 不做回收站/软删除机制

## Architecture

`FileDeleteTool` 遵循现有工具模式，放在 `edit_execute.py` 中，与 `FileReplaceTool`、`FileInsertTool` 并列。

```
FileDeleteTool.execute(args, ctx)
  ├─ anchor 非空 → _execute_text_replace(prefix=anchor, suffix=anchor, new_string="")
  │                 （复用 replace 的锚定校验逻辑）
  └─ anchor 为空 → _execute_line_delete(lineno)
                     （独立函数，行号直接定位，跳过锚定校验）
```

两条路径共享尾部换行处理逻辑（`new_string == ""` 时吃掉后续 `\n`），确保行为一致。

## Data Model

```
FileDeleteInput
├── file_path: str          (目标文件路径)
├── lineno: int (ge=1)      (要删除的行号，1-based)
└── anchor: str (default="") (目标行锚定子串，空字符串跳过锚定校验)
```

## API Contract

### delete

- **Tool id**: `delete`
- **Description**: Delete a single line from a file. Provide the line number from the latest read output and an anchor snippet to verify the target line. For deleting multiple consecutive lines, use replace instead. Read the target line first.
- **Parameters**: `FileDeleteInput` schema
- **Response**: `ToolResult` with diff, metadata `{"file", "operations", "start_line", "end_line"}`
- **Errors**:
  - Path traversal blocked
  - File not found
  - Line out of range
  - Staleness check failed
  - Read coverage check failed
  - Anchor mismatch

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 路径穿越 | `resolve_safe` 返回 None，报错 |
| 文件不存在 | `_resolve_edit_target` 报错 |
| 行号越界 | `_execute_line_delete` 中 lineno > len(lines) 时报错 |
| 文件被外部修改 | `check_staleness` 拦截，要求重新 read |
| 未读过目标行 | `check_read_coverage` 拦截 |
| 锚定子串不匹配 | `_find_text_segment` 报错 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 只支持单行删除 | 支持行范围/整文件删除 | 用户要求精简参数，大块删除走 replace |
| 单锚点 `anchor` | prefix + suffix 双锚点 | 单行只需一个锚点，双锚点是行范围的设计 |
| 有锚点走 `_execute_text_replace` | 独立实现 | 逻辑完全等价于 `new_string=""` 的 replace，避免重复代码 |
| 无锚点走独立 `_execute_line_delete` | 偷读行内容当 anchor 走 replace | 不应绕过用户意图——不传 anchor 就是想跳过校验；独立函数语义更清晰 |
| `anchor` 同时作为 prefix 和 suffix 传入 | 只传 prefix | `_find_text_segment` 要求 prefix/suffix 分别匹配首行和末行，单行场景首行=末行，anchor 同时满足两者 |

## Open Questions

- 无
