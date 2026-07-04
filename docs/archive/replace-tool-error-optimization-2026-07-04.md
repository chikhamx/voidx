> **Status: Done**

# Replace 工具报错优化 — 技术设计文档

## Context

replace 工具的主要消费者是 LLM agent，而非人类开发者。当前报错信息面向人类调试设计（`file_path=..., reason=..., start_no=...` 格式写入日志），但 agent 实际看到的是一段纯文本错误信息，缺少可操作的下一步建议。

通过对 `agent_events.jsonl` 中 20+ 条 `replace_failed` 事件的分析，归纳出 7 类失败模式。其中多数失败可以通过优化报错文本让 agent 自行修正重试，无需人工介入。

**核心矛盾**：报错描述了“什么错了”，但没有告诉 agent“怎么改”。

## Goals and Non-Goals

### Goals

- 每条 LLM 可见报错末尾附带一句**可操作建议**（重新读取目标行 / 调整 anchor / 改用多行替换）
- 歧义匹配时**列出候选行内容**，省去 agent 额外读取上下文
- anchor 未找到时**全文件搜索**，若唯一匹配则提示正确行号
- 跨行 anchor 误用时**解释原因**并建议改用多行替换
- 参数校验错误**精简**为首个缺失字段提示
- span 不匹配报错**用自然语言**表述
- 优化工具描述和字段描述，补充 anchor 匹配机制的说明
- 明确 LLM 可见文本的语义边界：只描述用户可执行动作和工具参数，不暴露 runtime 层实现细节

### Non-Goals

- 不改变匹配逻辑的核心容差参数（行搜索半径、span 容差）
- 不改变工具结果的结构契约（仍返回纯文本输出 + 错误 metadata）
- 不改变日志格式（`_log_replace_failure` 的诊断字段保持不变）
- 不处理 drift fallback 的歧义场景（已有独立报错路径）
- 不在 LLM 可见文本中暴露内部函数名、内部类型、日志字段或 runtime 控制流

## Architecture

所有改动集中在两个文件：

```
src/voidx/tools/file_ops/
├── edit_resolve.py    ← 报错文本生成（6 个返回点）+ 2 个辅助函数
└── edit_execute.py    ← 参数校验错误精简 + 工具/字段描述优化
```

改动分两类：

1. **报错文本增强**：在每个返回错误字符串的位置，构造增强后的报错文本
2. **描述优化**：更新 `FileReplaceTool.description` 和 `FileReplaceInput` 字段描述

不新增模块、不改变函数签名、不引入新依赖。

### 数据流

```
agent 调用 replace
  → replace 参数校验
  → 定位目标文本范围
  → 未定位到目标范围时返回增强后的错误文本
  → 日志记录保持原诊断格式不变
```

关键点：agent 只应该看到面向任务修正的自然语言错误文本。内部函数、metadata、日志、fallback、ctx 等 runtime 细节只属于实现层和诊断层，不进入 LLM 可见语义。

## LLM 可见文本契约

以下文本会进入 LLM 的语义上下文，必须按用户任务语言设计，而不是按 runtime 实现语言设计：

- `FileReplaceTool.description`
- `FileReplaceInput` 字段描述
- replace 失败时返回给 agent 的错误文本

这些 LLM 可见文本必须遵守以下约束：

- 只描述**可观察问题**：目标行未找到、anchor 不唯一、指定范围和当前文件不匹配等
- 只给出**可执行动作**：重新读取目标行、使用当前行号重试、加长 anchor、改用多行替换
- 可以提到工具参数名：`file_path`、`start_no`、`end_no`、`start_anchor`、`end_anchor`、`new_string`
- 不暴露 runtime 层词汇：`ToolResult`、`metadata`、`ctx`、`fallback`、`Pydantic`、内部函数名、日志事件名、异常类型
- 不暴露工具调用协议或 XML/JSON 细节；建议用自然语言描述“read lines X-Y”，不要生成伪调用语法
- 避免使用实现概念 `drift`；改为 “line numbers may have changed since the last read”

### 推荐文案风格

LLM 可见错误文本统一采用：

```
{问题描述}
{必要上下文：候选行/窗口内容}
Hint: {下一步动作，使用自然语言说明如何修正参数或重新读取上下文}
```

示例：

```
start_anchor 'target' was not found near line 2.
Lines around 2:
  1: old = 1
  2: current = 0
  3: target = 2
Hint: The anchor appears on line 3. Read lines 3-3, then retry replace with start_no=3 and a matching anchor.
```

## 工具描述与字段描述优化

当前 `FileReplaceTool.description` 和 `FileReplaceInput` 的字段描述缺少对匹配机制的说明，导致 agent 对 anchor 行为有错误预期。这是部分失败的根本原因——agent 在不了解机制的情况下构造了注定失败的请求。

### 工具描述（`FileReplaceTool.description`）

**当前**：
```
Replace whole lines in a file. Provide the exact start_no/end_no from the
latest read output, plus start_anchor/end_anchor substrings from the first
and last lines. Read the target lines first.
```

**问题**：
- 说 “exact” 但实际会在行号附近搜索 anchor，agent 不知道行号可以有小偏差
- 没提到 anchor 用于在行号附近搜索定位，agent 以为必须精确匹配
- 没提到单行替换 vs 多行替换的 anchor 行为差异
- “drift” 属于实现概念，不应出现在 LLM 可见描述里

**改进**：
```
Replace whole lines in a file. Use start_no/end_no from the latest read
output, plus start_anchor/end_anchor substrings from the first and last
lines to replace. Anchors are searched near the given line numbers in case
the file changed since the last read. Read the target lines first.
For single-line replace (start_no == end_no), both anchors must match the
same line. For multi-line replace, start_anchor matches the first line and
end_anchor matches the last line.
```

### 字段描述（`FileReplaceInput`）

**start_anchor / end_anchor**

**当前**：
```
Content anchor on the first line to replace — a substring expected anywhere
on that line. Use an empty string only when the first line is empty. Aim for
a distinctive snippet.
```

**问题**：
- 没说明 anchor 在行号附近搜索
- 没说明单行替换时两个 anchor 必须在同一行
- 空字符串的行为差异（单行信任行号 vs 多行搜索空行）未说明

**改进**：
```
Content anchor on the first line to replace — a substring expected anywhere
on that line. The anchor is searched near start_no in case the file changed
since the last read. For single-line replace, an empty anchor uses start_no
directly. For multi-line replace, use an empty anchor only when the first
line is empty. Aim for a distinctive snippet to avoid ambiguous matches.
```

（end_anchor 同理，将 “first line” 改为 “last line”，`start_no` 改为 `end_no`，单行替换时空 anchor 使用 `end_no` 直接定位。）

**new_string**

**改进**：
```
Replacement content. Use an empty string to delete the selected line or range.
```

LLM 只需要知道推荐删除语义：`new_string=""` 删除选中行或行段。实现层为了兼容单行替换中的 `"\n"` / `" "` 做了归一化，但这属于容错细节，不应写进 LLM 可见描述，避免诱导模型使用含糊输入。

**start_no / end_no**

**当前**：
```
Exact first line (1-based) to replace. Use the line number from the latest
read output.
```

**问题**：说 “Exact” 但实际会结合 anchor 在附近搜索。

**改进**：
```
First line (1-based) to replace. Use the line number from the latest read
output. The anchor is searched near this number in case the file changed
since the last read.
```

## 失败模式与改进方案

### 1. Anchor 未找到（最高频）

**当前报错**：
```
start_anchor 'missing' not found within ±3 lines of line 2.
Lines around 2:
1: target = 1
2: other = 0
3: target = 2
```

**问题**：agent 不知道 anchor 是否在文件其他位置，也不知道是否文件被改过。

**改进**：在全文件范围搜索 anchor，根据结果分三种情况：

| 全文件搜索结果 | 报错追加 |
|--------------|---------|
| 唯一匹配，在第 N 行 | `Hint: 'anchor' appears on line N. Read lines N-N, then retry replace with the refreshed line number and anchor.` |
| 多处匹配 | `Hint: 'anchor' appears on lines N1, N2, ... — provide a longer anchor to identify the target line.` |
| 完全不存在 | `Hint: 'anchor' was not found anywhere in the file. Check for typos or read the file again before retrying.` |

**实现位置**：全文件搜索逻辑统一封装在 `_anchor_not_found_message` 函数内（调用 `_global_anchor_search`）。该函数在 `prefix != ""` 且 `_find_line_candidates` 返回空列表时，被 `_find_single_line_segment`（第 112 行）和 `_find_text_segment`（第 57、61 行）调用。

### 2. 歧义匹配

**当前报错**：
```
single-line match ambiguous: lines 1, 3 all match anchors at the same distance from line 2. Provide a more specific start_anchor/end_anchor.
```

**问题**：只列行号不列内容，agent 必须额外读取上下文才能区分。

**改进**：列出候选行内容：
```
single-line match ambiguous: 2 candidate lines match anchors at equal distance from line 2:
  line 1: target = 1
  line 3: target = 2
Hint: Provide a longer start_anchor that uniquely identifies the target line.
```

**实现位置**：`_find_single_line_segment`（`len(best) > 1` 分支）

### 3. 跨行 Anchor 误用

**当前报错**：
```
start_anchor 'return' found but end_anchor 'offset' not on the same line within ±3 lines of line 1.
Lines around 1:
1:     return
2:     offset = 1
3:     pass
```

**问题**：agent 误以为单行替换可以分别指定首行和尾行的 anchor。报错说 “not on the same line” 但没解释为什么。

**改进**：
```
start_anchor 'return' matched near line 1, but end_anchor 'offset' is on a different line.
Single-line replace requires both anchors on the same line.
  line 1:     return
  line 2:     offset = 1
Hint: If you meant to replace multiple lines, use different start_no/end_no values so start_anchor matches the first line and end_anchor matches the last line.
```

**实现位置**：`_find_single_line_segment`（suffix 过滤后 `prefix_lines` 为空的分支）

### 4. Span 不匹配

**当前报错**：
```
no valid replace range found: candidate ranges did not match expected span 19 with less than 2 lines of drift. start_anchor candidates: 1; end_anchor candidates: 22.
```

**问题**：“expected span 19”“2 lines of drift” 对 agent 理解成本高，并且暴露了实现层术语。

**改进**：
```
No valid replace range found. You specified lines 1-20, but the closest
anchor match covers a different range.
start_anchor 'line 1' matched on line(s): 1
end_anchor 'line 22' matched on line(s): 22
Hint: Read the target block again, then retry replace with the current start_no/end_no and matching anchors.
```

**实现位置**：`_find_text_segment`（`ranked` 为空的分支）

### 5. 空行 Anchor 未找到

**当前报错**：
```
empty line not found within ±3 lines of start_no 2.
Lines around 2:
1: top
2: body
3: end
```

**问题**：agent 用空字符串作为 anchor 但目标行非空，报错没说清楚。

**改进**：
```
Empty-line anchor was not found near line 2 — line 2 is not empty.
Lines around 2:
  1: top
  2: body
  3: end
Hint: If the target line has content, use a substring from that line as start_anchor instead of an empty string.
```

**实现位置**：`_anchor_not_found_message`（`anchor == ""` 分支，第 153-162 行）。该函数在 `prefix != ""` 且 `_find_line_candidates` 返回空列表时被 `_find_single_line_segment` 和 `_find_text_segment` 调用。注意：`_find_single_line_segment` 中 `prefix == ""` 且 `target_line` 越界的分支返回的是独立的越界报错（`"line {target_line} out of range..."`），不走空行 anchor 路径。

### 6. 未读覆盖

**当前报错**：
```
Lines 3-3 in app.py must be read before editing.
```

**问题**：没有给出可操作的读取建议。

**改进**：
```
Lines 3-3 in app.py must be read before editing.
Hint: Read lines 3-3, then retry replace with the refreshed line numbers and anchors.
```

**实现位置**：`check_read_coverage` 返回值（`file_state.py`）或在 `edit_execute.py` 的 `_execute_text_replace` 中包装 coverage_error 后返回

### 7. 参数校验错误

**当前报错**（缺字段时返回 6 个字段的完整错误）：
```
Invalid arguments: 6 validation errors for FileReplaceInput
file_path
  Input should be a valid string ...
start_no
  Field required ...
end_no
  Field required ...
...（共 6 个字段）
```

**问题**：噪音过大，agent 需要解析长文本才能找到第一个缺失字段；同时 `FileReplaceInput` 属于内部类型，不应进入 LLM 可见错误文本。

**改进**：只返回首个错误，不提内部校验库或内部类型名：
```
Invalid arguments: field 'start_no' is required. Required fields: file_path, start_no, end_no, start_anchor, end_anchor, new_string.
```

**实现位置**：`edit_execute.py` 的 `FileReplaceTool.execute`（`except Exception as exc` 分支）

## API Contract

### 改动函数签名

所有函数签名不变。改动仅影响返回的**字符串内容**。

### 失败文本返回值

```
签名: (lines, start_no, end_no, prefix, suffix) -> tuple[int, int, int, int] | str
返回: 成功时返回 offset tuple；失败时返回增强后的错误字符串
```

错误字符串的新结构（统一模板）：
```
{问题描述}
{上下文：候选行/窗口内容}
Hint: {可操作建议}
```

注意：上述签名和内部返回类型只用于实现说明，不得照搬到 LLM 可见文案。

### 新增辅助函数

```python
def _global_anchor_search(lines: list[str], anchor: str) -> list[int]:
    """全文件搜索 anchor，返回所有匹配行号（1-based）。"""

def _format_candidate_lines(lines: list[str], line_numbers: list[int], max_count: int = 5) -> str:
    """格式化候选行内容，每行 '  N: content'，超长截断到 80 字符。"""
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| anchor 在窗口外但文件内唯一匹配 | 提示正确行号，建议读取该行后重试 |
| anchor 在文件内多处匹配 | 列出所有匹配行号，建议加长 anchor |
| anchor 完全不存在 | 提示检查拼写或重新读取文件 |
| 歧义匹配 | 列出候选行内容，建议加长 anchor |
| 跨行 anchor 误用 | 解释原因，建议改用多行替换 |
| span 不匹配 | 用自然语言表述差距，建议重新读取目标块 |
| 空行 anchor 目标非空 | 提示改用实际内容做 anchor |
| 未读覆盖 | 附带自然语言读取建议 |
| 参数校验失败 | 只返回首个缺失字段，不暴露内部校验类型 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 全文件搜索 anchor | 仅扩大搜索半径 | 全文件搜索能给出精确行号建议，从根本上解决行号偏移问题 |
| 候选行内容截断到 80 字符 | 不截断 | 与 `_window_snippet` 现有行为一致 |
| 参数校验只返回首个错误 | 返回所有错误但精简格式 | agent 逐个修正更高效，首个错误通常是根因 |
| 不改工具结果结构 | 增加结构化 suggestions 字段 | 保持向后兼容，输出文本已足够 |
| 不改日志格式 | 日志也加建议 | 日志面向人类调试已够用，agent 看的是 LLM 可见错误文本 |
| 读取建议用自然语言 | 生成伪工具调用语法 | 避免把工具调用协议泄露到提示词语义层，减少模型误学格式 |
| LLM 可见文本禁用 runtime 词汇 | 允许实现术语出现在报错中 | 报错应帮助 agent 修正任务参数，而不是让 agent 推断内部实现 |

## Open Questions

- [ ] 全文件搜索性能：对超大文件（>10000 行）是否需要限制搜索范围？当前 `_line_matches_replace_anchor` 是 O(n) 子串搜索，大文件可能有延迟。已知限制，暂不处理。
- [x] 读取建议采用自然语言，不在 LLM 可见报错中生成伪调用语法。
- [x] 多行替换的 span 不匹配报错改进是否需要列出候选 range 的内容？最终决定：只列行号不列内容（`_find_text_segment` 第 77-82 行已实现）。
