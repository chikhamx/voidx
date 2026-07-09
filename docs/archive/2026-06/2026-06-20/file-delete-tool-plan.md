> **Status: Done**

# FileDeleteTool — 实现计划

## Goal

在文件编辑工具集中新增 `delete` 工具，只支持单行删除，3 个参数。

## Architecture

在 `edit_execute.py` 中新增 `FileDeleteInput` + `FileDeleteTool`，统一走 `_execute_text_replace`。注册到 `ToolRegistry`。

## Tech Stack

- Python 3.11+, Pydantic, pytest

## File Structure

| 文件 | 变更 | 职责 |
|------|------|------|
| `src/voidx/tools/file_ops/edit_execute.py` | 修改 | 新增 `FileDeleteInput`, `FileDeleteTool` |
| `src/voidx/tools/file_ops/__init__.py` | 修改 | 导出 `FileDeleteInput`, `FileDeleteTool` |
| `src/voidx/tools/registry.py` | 修改 | 注册 `FileDeleteTool` |
| `tests/test_tools/test_file_ops_delete.py` | 新增 | delete 工具测试 |

## Tasks

- [x] 1. 在 `edit_execute.py` 中新增 `FileDeleteInput` + `FileDeleteTool`
  - `FileDeleteInput`: `file_path: str`, `lineno: int (ge=1)`, `anchor: str (default="")`
  - `FileDeleteTool`: `id="delete"`, execute 统一走 `_execute_text_replace`
  - 文件: `src/voidx/tools/file_ops/edit_execute.py`

- [x] 2. 更新 `__init__.py` 导出
  - 文件: `src/voidx/tools/file_ops/__init__.py`

- [x] 3. 更新 `registry.py` 注册
  - 文件: `src/voidx/tools/registry.py`

- [x] 4. 编写测试
  - 正常删除单行、锚点校验失败、未读过报错、staleness 拦截、尾部换行保留、metadata 完整性
  - 文件: `tests/test_tools/test_file_ops_delete.py`

## Tests

```bash
.venv/bin/python -m pytest tests/test_tools/test_file_ops_delete.py -v
.venv/bin/python -m pytest tests/test_tools/ -v
```

## Risks

- **anchor 同时作为 prefix/suffix**: `_find_text_segment` 对首行和末行分别校验 prefix/suffix，单行场景下首行=末行，anchor 需同时匹配两者，这是正确行为
