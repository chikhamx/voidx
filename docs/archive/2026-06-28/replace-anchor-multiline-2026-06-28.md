# replace 工具 anchor 跨行匹配问题分析

Date: 2026-06-28

> **Status: Done** — 容错方案已实现并测试通过。`_line_matches_replace_anchor`
> 入口对含 `\n` 的 anchor 做归一化（取首个非空行，纯 `\n` 归一化为 `""`），
> 覆盖四种情况的测试见 `tests/test_tools/test_file_ops_edit.py`。

## Context

LLM 调用 `replace` 工具时频繁报 "anchor ... not found within ±N lines" 错误，
LLM 自行描述为"anchor 跨行匹配失败"。经排查，代码中并无"跨行"字样，该措辞
是 LLM 对错误信息的自行转述。

根因不在替换逻辑本身，而在 anchor 匹配判定：当 LLM 传入的 `start_anchor` /
`end_anchor` 包含换行符 `\n` 时，逐行匹配永远失败。

## Current State

关键文件与调用链：

- `src/voidx/tools/file_ops/edit_execute.py:31-69` — `FileReplaceInput`，
  字段描述明确要求 anchor 是 "a substring expected anywhere on that line"
  （单行子串）。
- `src/voidx/tools/file_ops/edit_execute.py:84-98` — `execute` 入口，将
  `start_anchor` / `end_anchor` 原样传入 `_execute_text_replace`。
- `src/voidx/tools/file_ops/edit_resolve.py:149-163` — `_find_line_candidates`，
  在 `target_line ± TEXT_REPLACE_LINE_RADIUS`（默认 ±3）范围内逐行调用
  `_line_matches_replace_anchor` 收集候选行号。
- `src/voidx/tools/file_ops/edit_resolve.py:166-169` — `_line_matches_replace_anchor`，
  匹配判定核心：

  ```python
  def _line_matches_replace_anchor(line: str, snippet: str) -> bool:
      if snippet == "":
          return line == ""
      return snippet in line
  ```

- `src/voidx/tools/file_ops/read.py:25-31` — `_split_display_lines` 用
  `text.split("\n")` 切分文件内容，因此 `lines` 列表中每个元素都是**不含
  `\n`** 的纯行字符串。

匹配失败后的错误来源：

- `edit_resolve.py:56-62`（多行替换，`start_no != end_no`）：
  "start_anchor ... not found within ±N lines of start_no"。
- `edit_resolve.py:113-117`（单行替换，`start_no == end_no`）：
  "start_anchor ... not found within ±N lines of line"。

## Root Cause

`_line_matches_replace_anchor` 的 `snippet in line` 判定中，`line` 是单行
字符串（不含 `\n`）。当 LLM 传入的 `snippet`（anchor）包含 `\n` 时，`snippet
in line` 永远为 False，导致 `_find_line_candidates` 返回空列表，触发
"not found" 错误。

这是 LLM 的误用（传了多行 anchor），而非替换逻辑 bug。但 anchor 含 `\n` 是
高频误用模式，硬性拒绝不如容错处理。

## 四种 anchor 含 `\n` 的情况

匹配发生在 `_line_matches_replace_anchor(line, snippet)`，`line` 不含 `\n`，
`snippet` 是 LLM 传入的 anchor。

### 情况 1：anchor 首字符是 `\n`（如 `"\ndef foo"`）

- `snippet == ""` → False
- `snippet in line` → `"\ndef foo" in "def foo():..."` → False（line 不含 `\n`）
- 结果：报 "not found"
- 语义：LLM 想匹配 `def foo` 所在行，开头 `\n` 是误带（可能从上一行行尾
  复制而来）

### 情况 2：anchor 结尾是 `\n`（如 `"def foo\n"`）

- `snippet == ""` → False
- `snippet in line` → `"def foo\n" in "def foo():..."` → False
- 结果：报 "not found"
- 语义：LLM 想匹配 `def foo` 所在行，结尾 `\n` 是误带（行尾分隔符被算进
  了内容）
- 这是最常见的情况——LLM 从 read 输出复制行时容易带上行尾分隔符

### 情况 3：anchor 中间带 `\n`（如 `"def foo():\n    return 1"`）

- `snippet == ""` → False
- `snippet in line` → False（单行 line 不可能包含 `\n`）
- 结果：报 "not found"
- 语义：LLM 把多行内容当成一个 anchor 传入。它真正想定位的是首行
  `def foo():`（作为 start_anchor）或末行 `    return 1`（作为 end_anchor）

### 情况 4：anchor 是纯 `\n`（`"\n"`）

- `snippet == ""` → False（`"\n" != ""`）
- `snippet in line` → `"\n" in "some line"` → False
- 结果：报 "not found"
- 语义：LLM 想匹配一个空行。`"\n"` 是空行的"带分隔符表示"，等价于字段
  描述里 "the first line is empty" 场景，此时应当传 `""`

## 容错方案

### 统一归一化规则

四种情况的共同点：anchor 里的 `\n` 都是噪声，真正有意义的匹配内容是
anchor 的某一单行。容错策略是在 `_line_matches_replace_anchor` 入口对
snippet 做归一化——`snippet.split("\n")` 取**第一个非空行**，若全部为空
则归一化为 `""`。

逐情况验证：

| 情况 | 原始 snippet | `split("\n")` | 取首个非空行 | 归一化结果 | 匹配目标 |
|------|------------|--------------|------------|-----------|---------|
| 1 首部 `\n` | `"\ndef foo"` | `["", "def foo"]` | `"def foo"` | `"def foo"` | `def foo` 行 |
| 2 尾部 `\n` | `"def foo\n"` | `["def foo", ""]` | `"def foo"` | `"def foo"` | `def foo` 行 |
| 3 中间 `\n` | `"a\nb"` | `["a", "b"]` | `"a"` | `"a"` | 首行 `a` |
| 4 纯 `\n` | `"\n"` | `["", ""]` | 无非空行 | `""` | 空行 |

归一化后，`_line_matches_replace_anchor` 现有逻辑天然正确处理：

- 归一化结果 `""` → `snippet == ""` 分支 → `line == ""` → 匹配空行 ✓
- 归一化结果非空 → `snippet in line` → 正常子串匹配 ✓

### 实现位置

仅改 `_line_matches_replace_anchor`（`edit_resolve.py:166-169`）入口处加
归一化：

```python
def _line_matches_replace_anchor(line: str, snippet: str) -> bool:
    normalized = next((s for s in snippet.split("\n") if s != ""), "")
    if normalized == "":
        return line == ""
    return normalized in line
```

### 安全性

归一化只影响"匹配判定"（找候选行号），不触碰 `start_no` / `end_no` /
`new_string`。替换范围仍由 LLM 提供的行号决定，因此容错不会导致错误替换
——最坏情况是匹配到首行对应的行号，而这正是 `start_anchor` 字段语义
（"first line"）所期望的。

### 边界情况

情况 3 中，若 LLM 传的 `start_anchor` 是多行，但 `start_no` 指向的是末行
而非首行（LLM 的心智模型是"anchor 覆盖 start_no 到 end_no"），取首行会
匹配到错误的行。但这种情况罕见——`start_anchor` 字段描述明确是 "first
line"，且 `start_no` 也是 "first line"。取首行与字段语义一致，可接受。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| anchor 含首部 `\n` | 归一化剥离，取首个非空行匹配 |
| anchor 含尾部 `\n` | 归一化剥离，取首个非空行匹配 |
| anchor 中间含 `\n` | 归一化取首行匹配 |
| anchor 为纯 `\n` | 归一化为 `""`，匹配空行 |
| 归一化后仍无候选行 | 维持现有 "not found" 错误，不变 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 在匹配判定层容错（归一化 snippet） | 在错误信息层提示 LLM 改正 | 容错比硬性拒绝体验更好，且不改变替换语义 |
| 取首个非空行 | 取末行 / 取最长行 | 与 `start_anchor` 字段语义（"first line"）一致 |
| 纯 `\n` 归一化为 `""` | 单独处理空行分支 | 复用现有 `snippet == ""` → `line == ""` 逻辑，无需新增分支 |

## Open Questions

- [ ] 是否需要在错误信息中额外提示"anchor 已被归一化"，以便 LLM 知道其
      多行 anchor 被容错处理？当前方案不提示，保持错误信息简洁。
