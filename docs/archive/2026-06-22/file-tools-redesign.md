# 文件编辑工具集改造 — 技术设计文档

> **Status: Done**

## Context

当前 voidx 有 6 个文件工具（read/write/edit/insert/replace/delete），存在以下问题：

1. **write 和 edit 职责模糊**：write 是整文件覆盖，edit 是段落模糊匹配，两者与 insert/replace 功能重叠。LLM 经常在 write 和 edit 之间犹豫。
2. **缺少文件级操作**：创建空文件、删除文件、移动/重命名只能走 bash，不受文件安全机制（staleness/coverage/version）保护。
3. **insert 和 delete 是同一硬币的两面**：都是按行号操作——insert 在行号处插入，delete 删除行号处的行。拆成两个工具增加了 LLM 的选择成本。
4. **edit 的模糊匹配不可靠**：prefix/suffix ±100行搜索容易误匹配，与 replace（行号+锚点）功能高度重叠。

## Goals and Non-Goals

### Goals

- 新增 `file` 工具，支持文件级操作（create/delete/move）
- 新增 `line` 工具，合并 insert 和 delete 为按行号操作的单工具
- 保留 `replace` 不变
- 废弃 `write`、`edit`、`insert`、`delete`
- 写文件的新范式：`file create` + `line insert`
- 所有改动对权限系统、UI 层、workflow 层保持兼容，且 `replace` 继续按写文件工具受控

### Non-Goals

- 不改造 `read` 工具
- 不改造 `replace` 的 prefix/suffix 锚点机制
- 不引入目录级操作（mkdir/rmdir 走 bash）
- 不做 undo/redo 功能（现有 save_file_version 机制不变）

## Architecture

### 改造前后工具对比

| 改造前 | 改造后 | 变化 |
|--------|--------|------|
| `read` | `read` | 不变 |
| `write` | **废弃** | 功能由 `file create` + `line insert` 替代 |
| `edit` | **废弃** | 功能由 `replace` 替代 |
| `insert` | **废弃** | 合并到 `line`（op=insert） |
| `replace` | `replace` | 不变 |
| `delete`（单行+anchor） | **废弃** | 合并到 `line`（op=delete） |
| — | `file`（新增） | create/delete/move |
| — | `line`（新增） | insert/delete |

### 改造后工具集

```
文件级操作
├── read      — 读取文件内容（不变）
└── file      — 文件管理（create / delete / move）

行级操作
├── line      — 按行号插入/删除行（insert / delete）
└── replace   — 替换连续行（行号+锚点，不变）
```

4 个工具，2 个文件级 + 2 个行级，结构极简。

### 写文件新范式

改造前：`write(file_path, content)` 一步完成。

改造后：两步操作——

1. `file(file_path, op="create")` — 创建文件（如已存在则报错，需先 read 确认）
2. `line(file_path, op="insert", lineno=0, new_string=content)` — 写入内容

**为什么两步更好：**
- 强制 LLM 先确认文件状态（是否存在、是否需要覆盖），减少误覆盖
- line insert 天然受 read coverage 校验保护，write 没有
- 对于已有文件的修改，直接用 line/replace，不需要 write

**覆盖已有文件的流程：**
1. `read(file_path)` — 确认文件存在和内容
2. 优先用 `replace(file_path, ...)` 修改已读范围
3. 如确实要清空重写，使用 `file(file_path, op="create", overwrite=True)` 截断为空文件，再用 `line(file_path, op="insert", lineno=0, new_string=content)` 写入

**新建文件并写入的流程：**
1. `file(file_path, op="create")` — 创建空文件
2. `line(file_path, op="insert", lineno=0, new_string=content)` — 写入内容

**大文件写入流程：**
1. `file(file_path, op="create")` — 创建空文件
2. `line(file_path, op="insert", lineno=0, new_string=skeleton)` — 写入骨架（imports、类签名、prefix/suffix 标记）
3. `read(file_path)` — 读取骨架确认行号
4. `line` / `replace` — 增量填充实现块

**空文件 + line insert 的 coverage 处理：**
- `file create` 创建空文件后，`line(op="insert", lineno=0)` 走 `(0, 0)` 路径，**只在目标文件为空时跳过 read coverage 校验**
- 非空文件的 `line(op="insert", lineno=0)` 必须先 `read` 第 1 行；否则会绕过 read coverage 保护
- `file create` 内部会调用 `record_mtime` 和 `clear_read_coverage(path)`；不要依赖 `record_read_range(path, 1, 0)`，现有实现对 `end_line < start_line` 会直接返回
- 因此新范式下 `file create` → `line insert` 无需中间 read，流程顺畅，同时不会放宽已有文件的修改约束

## Data Model

### FileInput（新增）

```python
class FileInput(BaseModel):
    file_path: str = Field(description="Path to the file")
    op: Literal["create", "delete", "move"] = Field(
        description="File operation: create (create empty file + parent dirs), "
                    "delete (delete file), move (move/rename file)"
    )
    dest_path: str | None = Field(
        default=None,
        description="Destination path for move operation. Required when op=move."
    )
    overwrite: bool = Field(
        default=False,
        description="For create: overwrite if file exists. For move: overwrite destination."
    )
```

### LineInput（新增，合并 insert + delete）

```python
class LineInput(BaseModel):
    file_path: str = Field(description="Path to the file")
    op: Literal["insert", "delete"] = Field(
        description="Line operation: insert (insert content at lineno) or "
                    "delete (delete lines at lineno)"
    )
    lineno: int = Field(
        ge=-1,
        description=(
            "Line number (1-based). For insert: insert after this line "
            "(0 → beginning of file, -1 → end of file). "
            "For delete: first line to delete."
        ),
    )
    end_no: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For delete only: last line to delete (1-based). "
            "If omitted, deletes only the lineno line. "
            "Use the line number from the latest read output."
        ),
    )
    new_string: str = Field(
        default="",
        description=(
            "For insert only: content to insert. A trailing newline does not add "
            "an extra blank line; start with a newline only when an intentional "
            "blank first line is desired."
        ),
    )
```

## API Contract

### file — 文件管理工具

- **id**: `file`
- **op=create**:
  - 创建空文件，自动创建父目录
  - 文件已存在时：`overwrite=False` 报错，`overwrite=True` 截断为空文件
  - 覆盖时先走 `check_staleness` 和 `save_file_version`
  - 写入后走 `record_mtime`，并 `clear_read_coverage`
  - 返回：`File created: {path}` 或 `File overwritten: {path}`
- **op=delete**:
  - 只处理普通文件；目录删除不在本工具范围内
  - 删除前走 `check_staleness` 和 `save_file_version`
  - 删除文件，走 `save_file_version` 保存版本
  - 文件不存在时报错
  - 清除源路径的 read coverage 和 file mtime 记录
  - 返回：`File deleted: {path}`
- **op=move**:
  - 移动/重命名文件，自动创建目标父目录
  - 只处理普通文件；目录移动不在本工具范围内
  - 源文件和目标文件（如存在）都走 `check_staleness`
  - 走 `save_file_version`（源文件）；当 `overwrite=True` 且目标已存在时，也要保存目标文件版本
  - 源文件不存在时报错
  - 目标已存在时：`overwrite=False` 报错，`overwrite=True` 覆盖
  - 更新 read coverage 和 file mtime（源→目标），并清除源路径记录
  - 返回：`File moved: {source} → {dest}`

### line — 行级操作工具（合并 insert + delete）

- **id**: `line`
- **op=insert**:
  - 在 lineno 后插入 new_string 内容
  - lineno=0 → 文件开头，lineno=-1 → 文件末尾
  - 空内容直接跳过（返回 "No changes"）
  - 走 `check_read_coverage` 校验（仅空文件 lineno=0 插入跳过）
  - 走 `save_file_version` 保存版本
  - 走 `remap_read_coverage_from_file_diff` 更新覆盖范围
  - 返回 diff + line shift hints
- **op=delete**:
  - 删除 lineno 到 end_no 的行（end_no 省略时只删 lineno 一行）
  - 走 `check_read_coverage` 校验
  - 走 `save_file_version` 保存版本
  - 走 `remap_read_coverage_from_file_diff` 更新覆盖范围
  - 返回 diff + line shift hints
  - **无 anchor**：行号由 read 输出保证准确性
  - 对需要内容锚点确认的删除，继续使用 `replace(..., new_string="")`

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| file create 文件已存在且 overwrite=False | 返回错误，提示先 read 或设 overwrite=True |
| file delete 文件不存在 | 返回错误 |
| file move 源文件不存在 | 返回错误 |
| file move 目标已存在且 overwrite=False | 返回错误 |
| line delete 行号超出范围 | 返回错误，提示文件行数 |
| line delete 行号未读过 | 返回 coverage 错误，提示先 read |
| line insert 行号超出范围 | 返回错误 |
| line insert 行号未读过（非空文件非 lineno=0） | 返回 coverage 错误，提示先 read |
| line op=insert 但 new_string 为空 | 返回 "No changes"，不修改文件 |
| line op=delete 但 end_no < lineno | 返回参数校验错误 |
| 路径穿越 | 返回 "Path traversal blocked" |
| 文件被外部修改 | 返回 staleness 错误，提示重新 read |
| file op 目标是目录 | 返回错误，提示目录操作走 bash |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 废弃 write，用 file create + line insert 替代 | 保留 write | write 一步写整文件容易误覆盖；两步操作强制 LLM 确认文件状态，line insert 受 coverage 保护 |
| 废弃 edit，用 replace 替代 | 保留 edit | edit 的 prefix/suffix ±100行模糊匹配不可靠；replace 的行号+锚点更精确；减少工具数降低 LLM 选择成本 |
| 合并 insert + delete 为 line | 保留两个独立工具 | 两者都是按行号操作，op 参数区分语义清晰；减少工具数降低 LLM 选择成本；与 file 的 op 模式一致 |
| line delete 无 anchor | 保留 anchor | anchor 增加认知负担；行号由 read 输出保证；如需验证可走 replace 的 prefix/suffix |
| line delete 加 end_no 支持多行 | 新增单独的多行删除工具 | 同一工具更简洁，end_no 可选保持简单 |
| file 工具用 op 参数 | 拆成 file_create/file_delete/file_move 三个工具 | 一个工具减少注册数量；op 语义清晰；LLM 不需要在三个相似工具间选择 |
| file create 覆盖已有文件需 overwrite=True | 默认覆盖 | 安全优先，防止误覆盖；LLM 需要显式确认 |
| `replace` 继续归类为写文件工具 | 把 `replace` 留在旧 edit 权限外 | `replace` 仍会修改文件；权限、workflow gate、plan mode 必须继续拦截它 |
| file move 不处理目录 | 支持目录移动 | 目录操作复杂（递归、权限），保持简单；目录操作走 bash |

## 影响面与迁移

### 需要改动的文件

| 文件 | 改动 |
|------|------|
| `tools/file_ops/file.py` | **新增** — FileTool 实现 |
| `tools/file_ops/line.py` | **新增** — LineTool 实现（合并 insert + delete） |
| `tools/file_ops/edit_execute.py` | **删除** — FileEditTool、FileInsertTool、FileDeleteTool 全部废弃 |
| `tools/file_ops/write.py` | **删除** — 废弃 write |
| `tools/file_ops/edit_resolve.py` | 删除 `_resolve_paragraph_edits`、`_find_paragraph` 等 edit 专用逻辑；保留 `_find_text_segment`（replace 用） |
| `tools/file_ops/types.py` | 删除 EditEntry、ParagraphResolution；保留 ResolvedEdit、DisplayLines 等 |
| `tools/file_ops/__init__.py` | 更新导出 |
| `tools/registry.py` | 更新注册：去掉 write/edit/insert/delete，加入 file/line；去掉 `if t.id == "edit": continue` |
| `permission/rules.py` | `_FILE_PATTERN_TOOLS` 加入 "file"/"line"；`capability_for_tool` 加入 "file"/"line" → FILE_WRITE；EDIT_TOOLS 更新 |
| `permission/evaluate.py` | `EDIT_TOOLS` 更新：去掉 "write"/"edit"/"insert"/"delete"，加入 "file"/"line"，**保留 "replace"** |
| `ui/output/display_policy.py` | 去掉 "write"/"edit"/"insert"/"delete" 规则，加入 "file"/"line" 规则 |
| `ui/output/dock/nodes.py` | 更新 `_tool_display_name` 和 `_tool_display_value` 映射 |
| `ui/output/console/formatting.py` | 更新 `_fmt_args_short` 映射 |
| `ui/output/console/app.py` | 更新 `_TOOL_GERUND` 映射 |
| `ui/session.py` | `capture_tool_call` 加入 "file"/"line"，去掉 "write"/"edit"/"insert"/"replace" |
| `agent/graph/runtime_guards.py` | `normalize_tool_args` 更新工具名映射 |
| `tools/bash_router.py` | `_HintableTool` 和路由提示更新：write → file+line，insert → line；`_hint_write_redirect` 改为提示 file create + line insert；`_hint_write_heredoc` 同理 |
| `runtime/task_state.py` | `_WRITE_HINTS` 不含工具名，无需改 |
| `workflow/nodes.py` | 工具清单更新：去掉 "write"/"edit"/"insert"/"delete"，加入 "file"/"line"；`denied_tools` 去掉废弃工具并加入 "file"/"line"，**保留 "replace"** |

### bash_router 迁移细节

当前 `bash_router.py` 中有两处 write/insert 路由提示：

1. **`_hint_write_redirect`**（~L842）：检测 `> file` 或 `>> file` 重定向，提示用 `write` 或 `insert`
   - 改造后：`> file` → 提示 `file(op="create")` + `line(op="insert", lineno=0)`；`>> file` → 提示 `line(op="insert", lineno=-1)`
2. **`_hint_write_heredoc`**（~L894）：检测 heredoc 写入，提示用 `write` 或 `insert`
   - 改造后：同上逻辑

### Prompt 层影响

所有引用 write/edit/insert/delete 工具的 system prompt 需要更新为新的工具名和用法。主要在 LLM prompt 构建层。

## Test Plan

- `tests/test_tools/test_file_ops_line_file.py`
  - `file create` 创建空文件、拒绝覆盖、覆盖时保存版本并清 coverage
  - `file delete` 删除普通文件、保存版本、清除 coverage 和 mtime
  - `file move` 迁移 coverage/mtime；覆盖目标时保存源和目标版本
  - `line insert` 支持空文件 BOF、EOF 插入、非空 BOF 需 read coverage
  - `line delete` 支持单行和多行删除，未读行拒绝
- `tests/test_tools/test_tool_registry.py` / `tests/test_tools/test_tool_schemas.py`
  - registry 不再注册 `write/edit/insert/delete`
  - registry 注册并暴露 `file/line/replace`
  - `FileInput` / `LineInput` schema 与新 API 一致
- `tests/test_agent/test_permission.py`
  - `file` / `line` / `replace` 都归类为 `FILE_WRITE`
  - `edit` 权限配置继续覆盖 `file` / `line` / `replace`
  - read-only / plan mode 拒绝所有写文件工具
- UI / workflow / bash router focused tests
  - display policy、dock/console 显示名和 capture 记录新工具
  - workflow 写阶段工具清单使用 `file/line/replace`
  - bash router 对重定向和 sed 删除给出新工具提示

## Open Questions

- [x] file create 对已有文件且 overwrite=True 时，是否应该截断为空文件？→ **是**，截断为空文件。LLM 接着用 line insert 写入新内容，这是"覆盖重写"的语义。save_file_version 会保存旧版本。
- [x] file move 是否需要更新 file_mtimes 和 file_read_coverage 的映射？→ **是**，源文件的 mtime 和 coverage 应迁移到目标路径，源路径的记录清除。
