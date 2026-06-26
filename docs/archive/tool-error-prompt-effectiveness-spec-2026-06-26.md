# 工具错误提示有效性规范 — Tool Error Prompt Effectiveness Spec

> **Status: Done** — E1–E5 全部已实施，聚合测试 `tests/test_tools/test_tool_error_handling.py` 通过。

## Context

`tool-error-handling-spec-2026-06-26.md`（已归档）解决了错误返回的**机制层面**问题：
`model_validate` 裸调用、`error: True` 缺失、静默吞没。
本规范关注下一个层面：**错误提示对 LLM 是否有效**。

一个有效的错误提示需要同时满足两个条件：
1. **LLM 可读**：output 文字说清发生了什么、涉及哪个值、如何修正。
2. **机器可判**：metadata 结构化标记让调用方（`_tool_result_ok`、runtime guards）能正确区分成功与失败。

审查全部工具的错误返回路径后，发现 5 类问题影响提示有效性。
经核实消费方逻辑（`tool_executor/types.py:25-34`、`subagent.py:252-261`、`runtime_guards.py:388`），
部分问题实际影响低于初判，已在下方标注修正后的严重度。

## 消费方契约（已核实）

调用方通过以下逻辑判断工具结果是否成功：

```python
# tool_executor/types.py:25-34  (主 agent + subagent 共用)
def _tool_result_ok(result) -> bool:
    metadata = getattr(result, "metadata", {}) or {}
    if metadata.get("error") or metadata.get("blocked") or metadata.get("timeout"):
        return False
    if "exit_code" in metadata:
        return int(metadata.get("exit_code") or 0) == 0
    return True
```

```python
# runtime_guards.py:388  (fallback，仅在 result_ok=None 时生效)
ok = result_ok or (lambda result: not (getattr(result, "metadata", {}) or {}).get("error"))
```

**关键结论**：`_tool_result_ok` 检查 `error`、`blocked`、`timeout` 三个 key，
因此 bash 的 blocked/timeout 虽然缺 `error: True`，在主路径上不会被漏判。
但 `runtime_guards` 的 fallback 只检查 `error`，且 `mcp_servers/web.py:59` 也只检查 `error`。
为保持一致性，所有错误路径仍应设置 `error: True`。

## 问题清单

### E1：`bash` blocked / timeout / sandbox denial 缺 `error: True`

**严重度**：🟡 中（初判 🔴 高，经核实消费方逻辑后下调）

**位置**：

| 文件 | 行号 | 场景 | 修复后 metadata |
|------|------|------|----------------|
| `bash/tool.py` | 36-42 | `_check_command` 拦截 | `{"command": ..., "blocked": True, "error": True}` |
| `bash/tool.py` | 44-51 | `_sandbox_denial` 拦截 | `{"command": ..., "blocked": True, "error": True}` |
| `bash/tool.py` | 82-90 | 超时 | `{"command": ..., "exit_code": -1, "timeout": True, "error": True}` |
| `bash/tool.py` | 114-124 | 非零退出码 | 经 `payload` 组装，`error` 由调用方按 `ok` 判定 |

**问题**：这四条路径原本都没有 `error: True`。

**实际影响分析**：
- `_tool_result_ok`（主路径）检查 `blocked` 和 `timeout` key，所以前三个场景**不会被漏判**。
- 第四个场景（非零退出码）通过 `"exit_code" in metadata` 分支判断，也不会漏判。
- 但 `runtime_guards.py:388` 的 fallback 和 `mcp_servers/web.py:59` 只检查 `error` key，会漏判。
- 更重要的是**约定一致性**：其他所有工具的错误路径都有 `error: True`，bash 是唯一例外。

**实现**：在以上四条路径的 metadata 中添加 `"error": True`。

```python
# 实现（blocked 场景）
metadata={"command": inp.command, "blocked": True, "error": True}
```

### E2：`git` `_result()` 的 `error` 字段语义与其他工具不一致

**严重度**：🟡 中

**位置**：`git.py:919-946`

**问题**（修复前）：

```python
metadata={
    "command": command,
    "ok": ok,
    "error": not ok,        # 成功时为 False，失败时为 True
    "error_message": error.strip(),
}
```

其他工具的约定是：成功结果**不包含 `error` key**，失败结果 `error: True`。
git 的 `_result()` 在成功时也设置 `error: False`，导致 `metadata.get("error")` 在成功时返回 `False` 而非 `None`。

**实际影响**：
- `_tool_result_ok` 用 `if metadata.get("error")` 判断，`False` 是 falsy，不会误判。
- 但 `runtime_guards.py:388` 的 `not metadata.get("error")` 在成功时返回 `not False = True`，也正确。
- **无功能性 bug**，但违反"成功结果不含 error key"的隐含约定，增加认知负担。

**实现**：成功时不设置 `error` key，失败时设置 `error: True`。

```python
metadata = {"command": command, "ok": ok}
if not ok:
    metadata["error"] = True
    metadata["error_message"] = error.strip()
```

### E3：`git log -n abc` 的 ValueError 静默吞没

**严重度**：🟡 中

**位置**：`git.py:354-368`（三处 `int()` 转换）

**问题**（修复前）：

```python
if token == "-n" and i + 1 < len(rest):
    try:
        limit = min(int(rest[i + 1]), LOG_LIMIT_MAX)
    except ValueError:
        pass  # ← 静默忽略，默认用 limit=10
    skip_next = True
```

LLM 传 `git log -n abc` 时，参数被静默丢弃，返回 10 条 log。
LLM 以为请求成功且只有 10 条历史，不会意识到自己的参数格式有误。

**对 LLM 的影响**：意图被静默丢弃，LLM 无法自我纠正。

**实现**：在 output 或 metadata 中标注参数被忽略。

```python
except ValueError:
    limit_note = f"invalid -n value '{rest[i+1]}', defaulted to {limit}"
```

将 `limit_note` 传入 `_result()` 的 data 或 metadata 中。

### E4：`read` 的 "offset beyond EOF" 缺 `error: True`

**严重度**：🟢 低（初判 🟡 中，经核实 agent 层无"连续失败停止"逻辑后下调）

**位置**：`file_ops/read.py:224-230`

**问题**（修复前）：

```python
if start >= len(lines):
    return ToolResult(
        title=f"Read 0 lines",
        output=f"Offset {inp.offset} is beyond end of file (file has {len(lines)} lines).",
        metadata={"file": inp.file_path, "lines": 0, "total_lines": len(lines)},
    )
```

output 文字提示清晰，但 metadata 没有 `error: True`。

**对 LLM 的影响**：
- `_tool_result_ok` 会将此结果判定为**成功**（没有 error/blocked/timeout key，也没有 exit_code）。
- LLM 看到 "Read 0 lines" 和 "Offset beyond end of file"，文字层面能理解并能自我纠正。
- **备注**：初判假设"agent 层有连续失败则停止的逻辑会导致 LLM 反复尝试"，经核实 codebase 中
  `tool_executor/workflow.py:283` 和 `executor.py:306` 仅做单次 `result_ok` 判断，
  无连续失败计数器，因此该假设路径当前不存在。下调为🟢低，仍按约定一致性补齐 `error: True`。

**实现**：添加 `error: True` 和 `reason`。

```python
metadata={
    "file": inp.file_path, "lines": 0, "total_lines": len(lines),
    "error": True, "reason": "offset_beyond_eof",
}
```

### E5：`todo` 无 tracker 场景伪装成空列表

**严重度**：🔴 高（update 无 tracker）/ 🟡 中（read 无 tracker）

> **内部严重度区分**：`update` 是写操作，无 tracker 时返回只读语义的 "list is empty" 属操作语义错误，
> LLM 可能误以为更新已作用于空列表而非系统不可用，误导性更强，定为🔴高。
> `read` 无 tracker 误导性相对较低（只读操作返回空列表语义尚可接受），定为🟡中。

**位置**：

| 文件 | 行号 | 场景 | 修复后 output | 修复后 metadata |
|------|------|------|------------|--------------|
| `todo.py` | 89-95 | read 时无 tracker | "Todo tracker is not available..." | `{"error": True, "reason": "no_tracker", "todo_op": "read"}` |
| `todo.py` | 99-105 | read 时列表为空 | "Todo list is empty." | `{"todo_op": "read"}` |
| `todo.py` | 151-157 | update 时无 tracker | "Todo tracker is not available..." | `{"error": True, "reason": "no_tracker", "todo_op": "update"}` |
| `todo.py` | 160-166 | update 时列表为空 | "Todo list is empty." | `{"todo_op": "update"}` |

**问题**：四种不同状态（无 tracker vs 有 tracker 但空，read vs update）返回**完全相同的 output 和 metadata**。
LLM 无法区分"tracker 未初始化"（可能是配置问题）和"列表确实为空"（正常状态）。

**对 LLM 的影响**：
- 如果 tracker 因配置错误未注入，LLM 会以为 todo 系统正常但为空，不会报告问题。
- `update` 操作在无 tracker 时返回 "list is empty" 而非 "tracker not available"，语义错误。

**实现**：区分两种场景，无 tracker 时标记为 error。

```python
# 无 tracker（异常状态）
if self._tracker is None:
    return ToolResult(
        title="Todo: No tracker available",
        output="Todo tracker is not available in this runtime.",
        summary="error: no tracker",
        metadata={"error": True, "reason": "no_tracker", "todo_op": "read"},
    )

# 有 tracker 但列表为空（正常状态）
current_todos = self._tracker.get_todos()
if not current_todos:
    return ToolResult(
        title="Todo: Empty",
        output="Todo list is empty.",
        summary="empty",
        metadata={"todo_op": "read", "total": 0},
    )
```

## 实现状态

| 编号 | 问题 | 影响范围 | 状态 | 验证命令 |
|------|------|---------|------|---------|
| E1 | bash 缺 error:True | 4 处 | ✅ 已实施 | `pytest tests/test_tools/test_bash_tool.py -v` |
| E5 | todo 无 tracker 伪装空列表（update🔴/read🟡） | 4 处 | ✅ 已实施 | `pytest tests/test_agent/test_todo_events.py tests/test_agent/test_execute_tools_guard.py -v` |
| E3 | git log -n 静默吞没 | 3 处 | ✅ 已实施 | `pytest tests/test_tools/test_git_tool.py -k "log" -v` |
| E4 | read offset beyond EOF 缺标记 | 1 处 | ✅ 已实施 | `pytest tests/test_tools/test_file_ops_read.py tests/test_tools/test_file_ops_coverage_fingerprint.py -k "beyond" -v` |
| E2 | git _result error 约定不一致 | 1 处（影响全部 git 结果） | ✅ 已实施 | `pytest tests/test_tools/test_git_tool.py -v` |

> **聚合测试**：`pytest tests/test_tools/test_tool_error_handling.py -v`（23 项全部通过），
> 确认 `error: True` 约定在所有工具中一致。

## 做得好的地方（无需修改）

以下错误提示设计质量高，作为参考标准：

| 文件 | 场景 | 优点 |
|------|------|------|
| `file_ops/edit_resolve.py:50-86` | `_find_text_segment` 匹配失败 | 返回上下文窗口片段（`_window_snippet`），LLM 能直接看到实际内容修正 prefix/suffix |
| `agent.py:175-189` | ValidationError | 提取 missing 字段名单独提示，比泛泛的 `Invalid arguments` 更可操作 |
| `bash/tool.py:101-105` | 无输出且非零退出码 | 提示"interactive commands not supported, use non-interactive flags"，直接给修复方向 |
| `webfetch.py:160-168` | SSRF 防护 blocked | 明确说"resolves to private/internal address"，LLM 能理解是安全策略而非网络故障 |

## Non-Goals

- 不重构工具的错误处理架构（不引入 ErrorResult 类型）
- 不修改 `ToolExecutor` 层的 `_tool_result_ok` 判断逻辑
- 不处理 MCP 工具的错误提示（仅覆盖内置工具）
- 不修改 `runtime_guards.py:388` 的 fallback 逻辑（通过让所有工具设置 `error: True` 来兼容）
