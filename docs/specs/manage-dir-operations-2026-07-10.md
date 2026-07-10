---
name: manage-dir-operations
display_name: Manage 工具目录操作支持
description: 为 manage 工具增加显式目录操作，并定义根目录、符号链接、路径重叠和状态跟踪安全规则
doc_type: tech-design
audience: human+llm
---

# Manage 工具目录操作支持 — 技术设计文档

## TL;DR

给 `manage` 增加 `kind: Literal["file", "dir"] = "file"`。默认文件行为完全不变；显式传入 `kind="dir"` 后支持创建、递归删除和移动目录。

目录操作必须额外满足以下安全约束：

- delete 和 move 不得操作 workspace 根目录或任何 `sandbox_extra_paths` 根目录。
- 目录路径不得通过符号链接引用真实目录。
- move 的源目录树和目标目录树不得重叠。
- delete 和 move 成功后必须同时清理子文件的 `file_mtimes` 与 `file_read_coverage`。
- `kind="file"` 继续输出 `files`；目录操作输出 `directories`。
- 创建目录后不得提示调用 `write` 写入该目录。

## Context

`manage` 位于 `src/voidx/tools/file/manage.py`。当前 `_create_one`、`_delete_one` 和 `_move_one` 均拒绝目录：

| 操作 | 当前目录行为 | 原因 |
|------|-------------|------|
| create | error | 最终调用 `Path.write_text("")` |
| delete | error | 文件分支使用 `Path.unlink()` |
| move | error | `shutil.move()` 前存在人为 `is_dir()` 拦截 |

已有 `test_manage_delete_directory_returns_error` 验证默认文件模式遇到目录时返回 error。该测试必须保留，因为不传 `kind` 时仍是 `kind="file"`。

## Goals

- 支持 `create(kind="dir")` 创建空目录和父目录链。
- 支持 `delete(kind="dir")` 递归删除目录树。
- 支持 `move(kind="dir")` 移动或重命名目录。
- 对破坏性目录操作增加根目录、符号链接和目录树重叠保护。
- 删除或移动目录后清理失效的子文件读取状态。
- 保证所有现有文件调用、结果字段和文件模式输出文案不变。

## Non-Goals

- 不改变 `kind="file"` 的 create/delete/move 语义。
- 不支持只删除目录中的部分匹配文件。
- 不支持 `kind="symlink"`。
- 不为目录树创建版本备份或 staleness 快照。
- 不迁移目录内文件的 read coverage 到移动后的新路径；第一版采用保守清除。
- 不修改 `manage_display.py` 的 action title 与路径展示。

## Public API

### ManageInput

```python
class ManageInput(BaseModel):
    op: Literal["create", "delete", "move"]
    kind: Literal["file", "dir"] = "file"
    paths: str | list[str] | None = None
    moves: list[MoveSpec] | None = None
    overwrite: bool = False
```

Schema 文案同步从 file 扩展为 file or directory：

- `ManageTool.description` 明确列出 files/directories。
- `op`、`paths` 和 `moves` 的 Field description 使用 file or directory/path，而不是只写 file。
- `MoveSpec.src`、`MoveSpec.dest` 和 per-move `overwrite` 描述同时覆盖文件与目录。

`kind` 默认 `"file"`，旧调用和 legacy 参数归一化逻辑不变。

### Result compatibility

- 单项结果继续使用现有 `{"file": ..., "status": ...}` 结构，目录模式不另增 `dir` 字段。
- `_batch_result(operation, results, kind)` 根据 kind 选择名词：
  - `kind="file"`：保持 `Created 1/1 files` 等现有文案。
  - `kind="dir"`：使用 `Created 1/1 directories` 等文案。
- create 成功后的 `next_step_hint` 仅在 `kind="file"` 且原有条件满足时生成。

## Path Safety

### Preserve lexical path information

`resolve_safe()` 会调用 `Path.resolve()`，因此仅检查返回值无法知道调用者是否通过符号链接访问目录。目录分支必须在解析真实路径前同时检查原始词法路径和折叠 `.` / `..` 后的词法路径：

1. 相对路径以 workspace 拼接，绝对路径和 `~` 按现有规则展开，得到原始 lexical candidate。
2. 使用 `os.path.normpath()` 生成不访问文件系统、但会折叠 `.` / `..` 的 normalized candidate。
3. 分别遍历两个 candidate 的已有路径组件；任一组件是符号链接时返回 error，reason 包含 `symbolic link`。
4. 原始 candidate 检测 `link/../x` 等路径；normalized candidate 检测 `missing/../link` 等不存在前缀绕过。
5. 随后仍调用 `resolve_safe()` 完成真实路径解析与沙箱边界校验。

该规则适用于目录 create/delete 的路径，以及 move 的 source 和 destination。即使符号链接最终指向允许范围内，也拒绝目录操作，避免通过别名递归删除或覆盖真实目录。

### Protected roots

计算受保护根目录集合：

```python
protected_roots = {
    Path(ctx.workspace).resolve(),
    *(Path(p).expanduser().resolve() for p in ctx.sandbox_extra_paths),
}
```

以下情况返回 error，reason 包含 `root`，且不得修改文件系统或状态跟踪：

- `delete(kind="dir")` 的 resolved path 等于任一 protected root。
- `move(kind="dir")` 的 resolved source 等于任一 protected root。
- `move(kind="dir")` 的 resolved destination 等于任一 protected root，无论 overwrite 值为何。

`create(kind="dir")` 指向已存在的 protected root 仍按幂等 create 返回 skipped，因为该操作不删除或移动内容。

### Overlapping move trees

目录 move 在任何删除或 `mkdir` 前检查：

```python
source == dest
or dest.is_relative_to(source)
or source.is_relative_to(dest)
```

任一条件成立均返回 error：

- 相同路径：沿用 `Source and destination are the same file`，或改成兼容文件/目录的 same path 文案。
- destination 位于 source 内：reason 包含 `inside`。
- destination 是 source 的祖先：reason 包含 `overlap`。

第三种情况尤其重要：若 overwrite=True 时先删除 destination，会连同 source 一起删除。

## Operation Semantics

### create(kind="dir")

执行顺序：

1. 检查 lexical 路径中不存在符号链接组件。
2. 调用 `resolve_safe()`；越界返回 traversal error。
3. 路径存在且是文件：error，`Path is a file, not a directory`。
4. 路径存在且是目录：skipped，`directory already exists`。
5. 路径不存在：`path.mkdir(parents=True, exist_ok=True)`，返回 created。

`overwrite` 对目录 create 无意义并被忽略。目录 create 不调用 `record_mtime()`，也不生成 write next-step hint。

### delete(kind="dir")

执行顺序：

1. 完成 lexical symlink 检查与 `resolve_safe()`。
2. 路径不存在：skipped。文案可沿用现有 `file does not exist`，避免扩大兼容性变更。
3. 路径等于 protected root：error。
4. 路径不是目录：error，`Path is not a directory`。
5. 调用 `shutil.rmtree(path)`。
6. 无论成功还是抛出 `OSError`，都调用 `clear_tree_tracking(ctx, path)`：递归删除可能已经部分完成，原读取状态不再可信。
7. 成功返回 deleted；异常返回结构化 error。

目录 delete 不调用 `check_staleness()`、`save_file_version()` 或单文件 `clear_file_tracking()`。

### move(kind="dir")

执行顺序：

1. 对 source 和 destination 完成 lexical symlink 检查与 `resolve_safe()`。
2. source 不存在：skipped，沿用现有 source missing 文案。
3. source 或 destination 等于 protected root：error。
4. source 不是目录：error，`Source is not a directory`。
5. 检查 source/destination 目录树不重叠。
6. destination 存在且不是目录：error，`Destination is a file, not a directory`。
7. destination 存在且 overwrite=False：skipped。
8. 保存 `source_root` 与 `dest`，用于变更阶段后的状态清理。
9. destination 存在且 overwrite=True：`shutil.rmtree(dest)`。
10. `dest.parent.mkdir(parents=True, exist_ok=True)`。
11. `shutil.move(str(source), str(dest))`。
12. 一旦进入步骤 9–11 的文件系统变更阶段，无论成功还是抛出 `OSError`，都保守清理 source_root 与 dest 子树的跟踪状态。
13. 成功返回 moved；异常返回结构化 error。

目录 move 不调用 `check_staleness()`、`save_file_version()`、`record_mtime()` 或 `move_file_tracking()`。

`rmtree(dest)` 与 `move(source, dest)` 是两步操作，不保证原子性。若底层操作抛出 `OSError`，目录分支返回结构化 error，并清理可能失效的源/目标跟踪状态；不承诺恢复已删除的 destination。此限制必须保留在 Risks 中。

## Tracking State

在 `src/voidx/tools/file/state.py` 新增：

```python
def clear_tree_tracking(ctx: ToolContext, root: Path) -> None:
    resolved_root = root.resolve()
    for mapping in (ctx.file_mtimes, ctx.file_read_coverage):
        for key in list(mapping):
            if Path(key).is_relative_to(resolved_root):
                mapping.pop(key, None)
```

要求：

- 同时清理 `file_mtimes` 和 `file_read_coverage`。
- 必须使用 `Path.is_relative_to()`，不得使用字符串 `startswith()`；`/foo` 不能误清理 `/foobar`。
- delete 调用 `rmtree` 后无论成功或失败都清理目标子树，因为失败可能发生在部分删除之后。
- move 在进入覆盖删除、创建目标父目录或实际移动阶段后，无论成功或失败都清理源与目标子树。
- 静态校验阶段返回的 skipped 或 error 不清理状态；只有文件系统变更阶段可能使状态失效。
- 第一版不将 source coverage 重映射至 destination；移动后重新读取文件即可恢复状态。

## Files to Change

| 路径 | 改动 |
|------|------|
| `src/voidx/tools/file/manage.py` | 增加 kind；目录分支；根目录/符号链接/重叠保护；按 kind 输出名词；限制 write hint |
| `src/voidx/tools/file/state.py` | 新增 `clear_tree_tracking()` |
| `src/tests/test_tools/test_file_tools_redesign.py` | 增加目录行为、安全边界、状态清理和兼容性测试 |
| `src/voidx/ui/output/manage_display.py` | 不修改 |

## Invariants

- 未传 kind 等价于 `kind="file"`。
- 现有文件操作测试必须全部通过。
- 默认文件模式遇到目录仍返回 error。
- 所有目录路径仍受 `resolve_safe()` 沙箱边界限制。
- protected root 永远不能被目录 delete 或 move 作为源/目标操作。
- 目录操作不跟随符号链接。
- `shutil.rmtree()` 只用于目录 delete 和目录 move overwrite 预处理。
- 静态校验失败和 skipped 不改变文件跟踪状态；文件系统变更开始后的失败必须保守清理可能失效的目录树状态。
- 目录创建不生成 write 工具提示。

## Edge Cases

| 情况 | 预期行为 |
|------|---------|
| create 目录不存在 | 创建父目录链并返回 created |
| create 已存在目录 | skipped，幂等 |
| create 同名文件 | error，不破坏文件 |
| delete 空目录或非空目录 | 递归删除并清理子树跟踪 |
| delete 文件 | error，不删除文件 |
| delete workspace 或 extra root | error，不修改任何内容 |
| delete 符号链接目录 | error，不影响链接目标 |
| move source 不存在 | skipped |
| move source 不是目录 | error |
| move destination 是文件 | error |
| move destination 已存在，overwrite=False | skipped |
| move destination 已存在，overwrite=True | 替换目录并清理源/旧目标状态 |
| move destination 位于 source 内 | error |
| move destination 是 source 祖先 | error，不得先删除祖先 |
| move source 或 destination 是 protected root | error |
| move 任一 operand 含符号链接组件 | error |
| `/foo` 删除后的 tracking 清理 | 不影响 `/foobar` |

## Test Plan

测试类：`TestManageToolDirectoryOps`，位于 `src/tests/test_tools/test_file_tools_redesign.py`。

覆盖：

- schema 包含 kind，默认值为 file，description 可发现 directory 能力。
- create 单层/多层、重复创建、同名文件、无 write hint。
- delete 空目录、非空目录、文件、缺失路径。
- delete 拒绝 workspace root 与 extra allowed root。
- create/delete/move source/move destination 均拒绝符号链接路径。
- 覆盖直接链接、`link/../x` 和 `missing/../link` 等词法归一化绕过。
- delete 同时清理 descendant mtimes 和 coverage，且不误清理同前缀 sibling。
- move 重命名、创建目标父目录、overwrite false/true。
- move 拒绝 workspace root source/destination。
- move 拒绝 destination 位于 source 内或作为 source 祖先。
- move 清理 source 与原 destination 子树状态。
- file 模式 title 保持 `files`，dir 模式 title 使用 `directories`。
- 保留现有默认文件模式目录拒绝测试。

命令：

```bash
# 目录操作和 schema 测试
./test.py --backend -- src/tests/test_tools/test_file_tools_redesign.py \
  -k "TestManageToolDirectoryOps or test_manage_schema_uses_paths_and_moves" -v

# 全部 manage/write redesign 测试
./test.py --backend -- src/tests/test_tools/test_file_tools_redesign.py -v

# 完整 backend 回归
./test.py --backend -v
```

## Current RED Baseline

在目录实现提交前，新增测试应因以下原因失败：

- schema 尚无 kind。
- 目录操作仍进入文件分支并返回 `Path is a directory`。
- 目录 create 仍生成 write next-step hint。
- 目录模式尚无 `directories` 输出文案。

不得通过放宽断言、删除测试或将测试标记为 xfail 来消除 RED；后续实现应使其转为 GREEN。

## Risks / Trade-offs

| 风险 | 缓解 |
|------|------|
| `rmtree` 可递归删除大量数据 | 显式 kind、沙箱限制、protected root、symlink 与重叠保护 |
| overwrite move 为非原子两步操作 | 捕获 OSError 并明确不提供回滚保证；先完成全部静态安全检查 |
| 清除而非迁移 read coverage 会增加后续重读 | 保守正确优先，避免移动后使用失效行范围 |
| kind 被遗漏 | 默认文件模式维持当前安全行为，不形成回归 |
| 目录结果仍使用 `file` 字段命名 | 保持 metadata 兼容，后续如需统一路径字段另行设计 |
