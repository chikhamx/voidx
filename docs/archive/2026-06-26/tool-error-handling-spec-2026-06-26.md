# 工具错误提示规范 — Tool Error Handling Spec

> **Status: Done**

## Context

全面审查 `src/voidx/tools/` 下全部 39 个工具文件后发现，错误提示存在三类系统性问题：
参数校验异常未捕获、`error: True` metadata 标记缺失、错误被静默吞没。
本规范记录所有发现的问题并定义修复标准，作为后续实施的依据。

## 错误返回约定

voidx 工具的错误返回遵循以下契约（以 `file_ops/write.py` 为参考实现）：

```python
return ToolResult(
    output="<精简准确的错误描述>",      # 必填：面向 LLM 的错误说明
    metadata={"error": True, ...},     # 必填：error: True 标记 + 结构化原因
)
```

### 三条规则

1. **所有错误路径必须设置 `metadata={"error": True}`** — 调用方通过此标记区分错误与成功空结果。
2. **`model_validate(args)` 必须包裹 try/except** — 非法参数返回 `"Invalid arguments: {exc}"`，不暴露原始 traceback。
3. **错误消息精简准确** — 说明发生了什么、涉及哪个值，不泄漏内部实现细节。

---

## 问题清单

### P0：`model_validate` 裸调用（17 处，15 个文件）

`write.py`、`git.py`、`agent.py` 已正确包裹 `model_validate`。以下工具直接裸调用，传入非法参数时抛出原始 Pydantic traceback：

| 文件 | 行号 | 工具类 |
|------|------|--------|
| `file_ops/read.py` | 182 | `FileReadTool` |
| `file_ops/file.py` | 52 | `FileTool` |
| `file_ops/edit_execute.py` | 85 | `FileReplaceTool` |
| `search.py` | 35 | `GlobTool` |
| `search.py` | 139 | `GrepTool` |
| `lsp.py` | 56 | `LspTool` |
| `lsp.py` | 102 | `LspFormatTool` |
| `bash/tool.py` | 30 | `BashTool` |
| `todo.py` | 75 | `TodoTool` |
| `task_status.py` | 33 | `TaskStatusTool` |
| `clarify.py` | 48 | `ClarifyTool` |
| `workflow.py` | 88 | `WorkflowTool` |
| `plan_checkpoint.py` | 70 | `PlanCheckpointTool` |
| `compact_context.py` | 40 | `CompactContextTool` |
| `webfetch.py` | 142 | `WebFetchTool` |
| `websearch.py` | 182 | `WebSearchTool` |
| `skills.py` | 72 | `SkillsTool` |
| `load_doc_template.py` | 38 | `LoadDocTemplateTool` |

**修复模式**：
```python
async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
    try:
        inp = XxxInput.model_validate(args)
    except Exception as exc:
        return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
    # ... 正常逻辑
```

### P1：`error: True` metadata 缺失（8 处）

以下错误返回遗漏了 `metadata={"error": True}` 标记，调用方无法区分错误与成功结果：

#### `search.py` — 3 处

| 行号 | 错误消息 | 问题 |
|------|---------|------|
| 143 | `Path traversal blocked: {inp.path}` | 缺 `error: True`（对比 `file_ops/write.py:66` 同类错误有标记） |
| 150 | `Invalid regex: {e}` | 缺 `error: True` |
| 216 | `Path not found: {inp.path}` | 缺 `error: True`（对比 `file_ops/file.py:68` 有标记） |

#### `agent.py` — 3 处

| 行号 | 错误消息 | 问题 |
|------|---------|------|
| 198-200 | `Child agent execution not available` (无 resolver) | 缺 `error: True`（对比行 188、194 有标记） |
| 205 | `Unknown child agent: {inp.agent}` | 缺 `error: True` |
| 210-212 | `Child agent execution not available` (无 runner) | 缺 `error: True` |

#### `task_status.py` — 2 处

| 行号 | 错误消息 | 问题 |
|------|---------|------|
| 36 | `Task tracker not available.` | 缺 `error: True`、`title`、`summary` |
| 41 | `Task not found: {inp.task_id}` | 缺 `error: True`、`title`、`summary` |

### P2：错误静默吞没（5 处）

#### `git.py` — 辅助函数返回空列表掩盖失败

| 行号 | 函数 | 问题 |
|------|------|------|
| 872-873 | `_staged_files()` | git diff 失败时 `return []`，调用方无法区分"无文件"与"命令失败" |
| 879-880 | `_unstaged_files()` | 同上 |
| 886-887 | `_commit_files()` | 同上 |

**修复方向**：返回 `tuple[list[str], str | None]`（文件列表 + 错误信息），或在失败时 log warning 并在调用方上下文中标注。

#### `git.py:488` — `git show` 错误诊断不准确

```python
# 当前：硬编码 ref_not_found，丢弃 stderr
return _result("show", ctx, repo=repo, ok=False, error="ref_not_found")
```

`git show` 失败可能是权限问题、仓库损坏等，不仅是 ref 不存在。stderr 被丢弃。

**修复**：`error=meta_proc["stderr"].strip() or meta_proc["stdout"].strip() or "ref_not_found"`

#### `search.py:208` — PermissionError 静默 pass

```python
except PermissionError:
    pass  # 无日志、无提示，用户只看到 "No matches found"
```

**修复**：至少 `log_tool_event` 记录被跳过的目录，或在结果 metadata 中标注 `skipped_dirs` 计数。

### P3：metadata 约定不一致

#### `todo.py` — `error` 字段值类型不一致

| 行号 | 当前值 | 问题 |
|------|--------|------|
| 145 | `metadata={"error": "updates_required"}` | 字符串，其他工具用布尔 `True` |
| 221 | `metadata={"error": "todos_required"}` | 同上 |
| 237 | `metadata={"error": "duplicate_ids", ...}` | 同上 |

此外，"无 tracker"和"空列表"场景（行 87-92、97-102、148-154、157-163）返回 `metadata={"todo_op": "..."}` 无 error 标记，看起来像成功。

**修复**：统一为 `metadata={"error": True, "reason": "updates_required"}`，对真正的错误场景补 error 标记。

#### `task_status.py:58` — 访问私有属性

```python
metadata={"running": len(running), "total": len(self._tracker._tasks)}
# 应改为：len(self._tracker.list_all())
```

行 55 已用公共方法 `list_all()`，行 58 却访问私有 `_tasks`，不一致且脆弱。

### P4：低优先级

| 文件 | 行号 | 问题 |
|------|------|------|
| `git.py` | 354-368 | `git log -n abc` 的 `ValueError` 被静默吞没，默认用 limit=10 无提示 |
| `read.py` | 221-227 | "offset beyond EOF" 未标记 `error: True`（可争论这不是 error） |
| `file_ops/*` | 多处 | `read_text()`、`write_text()`、`shutil.move()` 等 I/O 未包裹 try/except，OS 级失败抛原始异常 |

---

## 修复优先级

| 优先级 | 类别 | 影响范围 | 修复复杂度 |
|--------|------|---------|-----------|
| P0 | `model_validate` 裸调用 | 17 处，15 个文件 | 低（模式固定） |
| P1 | `error: True` 缺失 | 8 处 | 低 |
| P2 | 静默吞没错误 | 5 处 | 中（需设计返回结构） |
| P3 | metadata 不一致 | todo.py + task_status.py | 低 |
| P4 | 低优先级 | 3 处 | 低 |

## Non-Goals

- 不重构工具的错误处理架构（不引入新的 ErrorResult 类型）
- 不修改 `ToolExecutor` 层的兜底错误处理
- 不处理 MCP 工具的错误提示（仅覆盖内置工具）
