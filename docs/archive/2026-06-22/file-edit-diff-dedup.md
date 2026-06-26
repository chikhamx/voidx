> **Status: Done**
# 文件编辑工具上下文去重 — 技术设计文档

## Context

LLM 调用文件编辑工具时，`new_string`/`content` 作为入参发送；工具返回的 `result.output` 包含完整 unified diff，diff 中的 `+` 行与入参内容完全重复。对于大段编辑（如 `write` 写 150 行），同一内容在上下文中出现两次，浪费 token。

**优化方向**：在 prune 阶段，将历史 AIMessage 中文件编辑工具的大字段参数替换为操作摘要，保留 ToolMessage 中的完整 diff。这样 LLM 上下文中只有 diff 一份内容，没有入参的重复。

当前链路：
1. LLM 发出 tool_call：`replace(new_string="def hello():\n    print('hi')\n")`
2. Runtime 返回 tool_result：`output` 包含完整 diff（含 `+` 行 = `new_string`）
3. 两者都留在上下文中 → `new_string` 内容出现两次

优化后：
1. LLM 发出 tool_call：`replace(new_string="def hello():\n    print('hi')\n")`（当轮不变）
2. Runtime 返回 tool_result：完整 diff（不变）
3. **prune 阶段**：tool_call 的 `new_string` → `"[omitted: see diff in tool result]"`，tool_result 的 diff 保留

## Goals and Non-Goals

### Goals
- 在 prune 阶段精简历史 AIMessage 中文件编辑工具的大字段参数（`new_string`、`content`）
- 保留 ToolMessage 中的完整 diff，LLM 仍能确认编辑结果
- 对 `write`、`replace`、`line(insert)` 三个有内容入参的工具统一处理
- 只精简超过阈值的大字段，短内容保留原值

### Non-Goals
- 不修改当轮的 tool_call 参数
- 不修改工具入参 schema
- 不修改 `result.output` 或 `result.diff` 的内容
- 不修改 compaction（Layer 3）逻辑
- 不处理 `edit`/`insert`/`delete` 工具（未暴露给 LLM）

## Architecture

### 改动点

在 `CompactionService.prune()` 中新增对 AIMessage 的处理：遍历历史 AIMessage 的 `tool_calls`，对文件编辑工具的大字段参数替换为摘要。

```
当前 prune:  只处理 ToolMessage.content（截断旧工具输出）
新增 prune:  同时处理 AIMessage.tool_calls[*].args 中的大字段
```

### 精简规则

| 工具 | 参数 | 精简条件 | 精简为 |
|------|------|---------|--------|
| `write` | `content` | `len(content) > len(placeholder)` | `"[omitted: N lines written]"` |
| `replace` | `new_string` | `len(new_string) > len(placeholder)` | `"[omitted: see diff in tool result]"` |
| `line` (op=insert) | `new_string` | `len(new_string) > len(placeholder)` | `"[omitted: see diff in tool result]"` |
| `line` (op=delete) | 无大字段 | — | 不处理 |

阈值说明：占位符 `"[omitted: see diff in tool result]"` 约 36 字符，`"[omitted: N lines written]"` 约 28 字符。只有原始内容长度超过占位符长度时才精简，否则反而浪费 token 或增加 LLM 推理负担。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `src/voidx/llm/compaction.py` | `prune()` 中新增 AIMessage tool_calls args 精简逻辑 |

### 实现细节

在 `prune()` 的遍历循环中，对 AIMessage 也做处理。核心逻辑提取为辅助函数 `_prune_ai_tool_call_args`：

```python
# prune() 遍历循环中新增
if (
    isinstance(msg, AIMessage)
    and turns_seen >= 1  # 不在当前 turn 内
    and hasattr(msg, "tool_calls")
    and msg.tool_calls
):
    new_tcs, saved = _prune_ai_tool_call_args(msg.tool_calls, messages, i)
    if new_tcs is not None:
        ai_to_rebuild[i] = new_tcs
        pruned_chars += saved
    continue
```


`_prune_ai_tool_call_args` 对每个 tool_call 做精简，返回 `(new_tool_calls, saved_chars)` 元组：

```python
def _prune_ai_tool_call_args(tool_calls, messages, ai_msg_index):
    changed = False
    saved_chars = 0
    new_tool_calls = []
    for tc in tool_calls:
        tc_copy = {**tc, "args": dict(tc.get("args", {}))}
        args = tc_copy["args"]
        name = tc.get("name", "")
        tc_id = tc.get("id", "")

        if name == "write" and "content" in args:
            placeholder = f"[omitted: {args['content'].count(chr(10)) + 1} lines written]"
            if len(args["content"]) > len(placeholder) and _tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["content"]) - len(placeholder)
                args["content"] = placeholder
                changed = True
        elif name == "replace" and "new_string" in args:
            placeholder = PRUNE_ARGS_PLACEHOLDER_DIFF
            if len(args["new_string"]) > len(placeholder) and _tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["new_string"]) - len(placeholder)
                args["new_string"] = placeholder
                changed = True
        elif name == "line" and args.get("op") == "insert" and "new_string" in args:
            placeholder = PRUNE_ARGS_PLACEHOLDER_DIFF
            if len(args["new_string"]) > len(placeholder) and _tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["new_string"]) - len(placeholder)
                args["new_string"] = placeholder
                changed = True
        new_tool_calls.append(tc_copy)
    return (new_tool_calls if changed else None, saved_chars)
```

`_tool_result_has_diff` 检查对应 ToolMessage 是否包含 diff 标记（`---` 和 `+++`），确保只在有 diff 替代时才精简 args。搜索范围从 `ai_msg_index` 向前直到下一个 HumanMessage（turn boundary），而非硬编码偏移量，确保跨 turn 不会误匹配。编辑失败（无 diff）的 tool_call 不会被精简。

关键点：
- `turns_seen >= 2`：反向遍历中，过了 2 个 HumanMessage 边界后的消息属于旧 turn，应被精简
- 只精简超过阈值的大字段，短内容保留原值
- 只在对应 ToolMessage 包含 diff 时精简，编辑失败（无 diff）的 args 保留原值
- **用 `model_copy` 重建 AIMessage**而非 in-place mutation，保留 `response_metadata`/`usage_metadata`/`id` 等属性（见下文）
- `line` 工具需额外判断 `op=insert`，避免误处理 `op=delete`

### 为什么重建 AIMessage 而非 in-place mutation

现有 `prune()` 对 ToolMessage 使用 `messages[idx] = type(messages[idx])(content=truncated, ...)` 重建对象，替换 list 中的引用。对 AIMessage 应保持一致。

更重要的是，`prune()` 在 `turn_runner.py:290` 被调用，操作的是 `final["messages"]`，而**持久化发生在 prune 之后**（`line 308-334`，`msg.tool_calls` 被直接写入 session）。如果 in-place mutation 修改了 `tool_calls` 的 args dict，持久化到 session 的数据也会被污染——历史 tool_call 参数会丢失原始值。

重建方式：先深拷贝 `tool_calls`（`{**tc, "args": dict(tc["args"])}`），对拷贝做精简，再用 `model_copy(update={"tool_calls": new_tcs})` 重建 AIMessage。使用 `model_copy` 而非 `AIMessage(...)` 构造器是因为后者会丢失 `response_metadata`、`usage_metadata`、`id` 等属性。

### 上下文效果对比

**Before**（prune 后，tool_call 和 tool_result 都保留完整内容）:
```
# AIMessage tool_call:
replace(file_path="foo.py", start_no=10, end_no=12, new_string="def hello():\n    print('hi')\n")

# ToolMessage tool_result:
File edited: foo.py (1 operations)
--- a/foo.py
+++ b/foo.py
@@ -10,3 +10,3 @@
-def old_func():
-    pass
+def hello():
+    print('hi')
```

**After**（prune 后，tool_call 的 new_string 被精简，tool_result 的 diff 保留）:
```
# AIMessage tool_call:
replace(file_path="foo.py", start_no=10, end_no=12, new_string="[omitted: see diff in tool result]")

# ToolMessage tool_result:
File edited: foo.py (1 operations)
--- a/foo.py
+++ b/foo.py
@@ -10,3 +10,3 @@
-def old_func():
-    pass
+def hello():
+    print('hi')
```

LLM 从 diff 中仍能看到完整的新旧内容，tool_call 中的 `new_string` 不再重复。

**write 工具 Before**（150 行新建文件）:
```
# tool_call: write(content="import os\n...150行...\n")
# tool_result: File written: foo.py (4500 bytes)\n--- /dev/null\n+++ b/foo.py\n@@ -0,0 +150 @@\n+import os\n...150行...
```

**write 工具 After**:
```
# tool_call: write(content="[omitted: 150 lines written]")
# tool_result: File written: foo.py (4500 bytes)\n--- /dev/null\n+++ b/foo.py\n@@ -0,0 +150 @@\n+import os\n...150行...
```

**短内容不精简**（`new_string` 长度 < 占位符长度）:
```
# tool_call: replace(new_string="pass")  — 保留原值，不替换
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| tool_calls 为空或格式异常 | 跳过，不处理 |
| args 中无 `new_string`/`content` 字段 | 跳过，不处理 |
| prune 触发条件不满足（当前 turn） | 不执行任何精简 |
| 内容长度未超过阈值 | 保留原值，不精简 |
| 编辑失败（ToolMessage 无 diff） | 保留原值，不精简 args（因为没有 diff 替代） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 在 prune 阶段精简 tool_call args | 在工具返回时精简 diff（去 `+` 行） | 保留完整 diff 给 LLM 更有价值（diff 同时包含新旧内容），精简入参更安全 |
| 重建 AIMessage 对象 | in-place mutation 修改 args dict | prune 后持久化会写入 session，in-place mutation 会污染持久化数据；与现有 ToolMessage 重建模式一致 |
| 只处理 `write`/`replace`/`line` | 处理所有工具的 args | 只有文件编辑工具有大字段重复问题，其他工具的 args 很小 |
| 不处理 `edit`/`insert`/`delete` | 也处理 | 这些工具未暴露给 LLM，不存在上下文重复问题 |
| 用 `[omitted: ...]` 占位 | 直接删掉字段 | 删字段可能导致 schema 校验失败，占位更安全 |
| 加精简阈值（内容长度 > 占位符长度） | 无条件精简 | 短内容精简后反而增加推理负担，占位符本身也占 token |
| 只在 ToolMessage 有 diff 时精简 | 无条件精简 | 编辑失败时没有 diff 替代，精简 args 会丢失信息 |
| `turns_seen >= 1` 而非 `>= 2` | `turns_seen >= 2` | args 精简不丢失信息（diff 替代），上一 turn 结束即可精简，无需等 2 turn |
| 用 `model_copy` 重建 AIMessage | `AIMessage(...)` 构造器 | 构造器会丢失 `response_metadata`/`usage_metadata`/`id` 等属性；`model_copy` 保留所有原始属性 |
| `_tool_result_has_diff` 搜索到下一个 HumanMessage | 硬编码偏移量 `+5` | 硬编码偏移量可能跨 turn 误匹配，或漏掉间隔较远的 ToolMessage；以 HumanMessage 为边界更准确 |
| `pruned_chars` 计入 args 节省量 | 只计入 ToolMessage 截断量 | args 精简同样减少上下文 token，应纳入 prune 总量以准确反映节省效果 |

## Open Questions

（无）