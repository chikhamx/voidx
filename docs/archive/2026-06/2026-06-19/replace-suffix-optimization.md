# Replace 工具 Suffix 定位优化 — 技术设计文档

> **Status: Done**

## Context

replace 工具使用 prefix + suffix 双锚点定位要替换的文本段。LLM 调用时 suffix 频繁出错：

1. **suffix 匹配到错误位置**：`text.find(suffix, prefix_offset + len(prefix))` 取第一个匹配，但文件中 suffix 可能多次出现
2. **suffix 完全找不到**：LLM 提供的 suffix 与文件实际内容有细微空白差异
3. **prefix == suffix 时行为诡异**：单行替换场景，suffix 容易匹配到 prefix 自身

根本原因：**suffix 搜索没有利用行号信息，且当前语义是字符级截取**。prefix 搜索用 `lineno` 做距离排序消歧，但 suffix 只做 `text.find()`。当文件中有重复代码模式时，suffix 极易命中错误位置；当 suffix 只匹配到行中间时，还会把行尾残留拼到 `new_string` 后面。

## Goals and Non-Goals

### Goals

- 将 replace 的定位语义改为行级范围：`prefix` 定位起始行，`suffix` 定位结束行
- 新增 `start_no` / `end_no` 参数，分别表示 prefix / suffix 的目标行号
- 在 `start_no ±3` 和 `end_no ±3` 内收集候选行，组合成候选区间后统一评分消歧
- 对外契约要求调用方提供准确的 `start_no` / `end_no`；搜索半径只是 runtime 对 LLM 行号漂移的兜底
- 替换范围为起始行行首到结束行行尾，避免字符级 suffix 残留
- 允许 `new_string` 为任意行数；行数变化由现有 diff/read coverage remap 处理

### Non-Goals

- 不改变 edit 工具的 paragraph 定位逻辑
- 不做模糊匹配/正则匹配（过度宽松反而危险）
- 不要求 `prefix` 是起始行开头，也不要求 `suffix` 是结束行结尾；它们只需是对应行内 substring

## Architecture

### 参数变更

**现状** (`FileReplaceInput`)：
```
file_path: str
lineno: int          # prefix 的大致行号
prefix: str
suffix: str
new_string: str
```

**改为**：
```
file_path: str
start_no: int        # prefix 所在起始行的准确行号（1-based）
end_no: int          # suffix 所在结束行的准确行号（1-based）
prefix: str
suffix: str
new_string: str
```

- `prefix`：起始行内任意 substring
- `suffix`：结束行内任意 substring
- `prefix` / `suffix` 为空字符串时，只匹配对应的空行，不作为通配符
- `new_string`：替换内容，允许任意行数

### 核心改动：`_find_text_segment` 签名和逻辑

**现状**：
```python
def _find_text_segment(lines, lineno, prefix, suffix):
    # prefix: _find_snippet_matches → lineno 距离消歧
    # suffix: text.find(suffix, prefix_offset + len(prefix))  # 无消歧
```

**改为**：
```python
def _find_text_segment(lines, start_no, end_no, prefix, suffix):
    # prefix_candidates: start_no ±3 内所有包含 prefix 的行
    # suffix_candidates: end_no ±3 内所有包含 suffix 的行
    # candidate_pairs: prefix_candidates × suffix_candidates
    # 过滤方向和跨度后，按整体距离选唯一最优 pair
    # 返回整行范围的字符偏移
```

### 数据流

```
_find_text_segment(lines, start_no, end_no, prefix, suffix)
  ├── 1. 收集起始候选
  │     └── 在 [start_no - 3, start_no + 3] 内找所有包含 prefix 的行
  │
  ├── 2. 收集结束候选
  │     └── 在 [end_no - 3, end_no + 3] 内找所有包含 suffix 的行
  │
  ├── 3. 组合候选区间
  │     └── candidate_pairs = prefix_candidates × suffix_candidates
  │
  ├── 4. 过滤不可信区间
  │     ├── suffix_line < prefix_line → 丢弃
  │     └── abs((suffix_line - prefix_line) - (end_no - start_no)) >= 2 → 丢弃
  │
  ├── 5. 选择最优区间
  │     ├── score = (
  │     │     abs(prefix_line - start_no) + abs(suffix_line - end_no),
  │     │     abs((suffix_line - prefix_line) - (end_no - start_no)),
  │     │     abs(prefix_line - start_no),
  │     │     abs(suffix_line - end_no),
  │     │   )
  │     ├── score 最小且唯一 → 命中
  │     ├── 无候选 → 返回诊断错误
  │     └── 最小 score 并列 → 返回 ambiguous
  │
  └── 6. 返回整行范围 (start_offset, end_offset, start_line, end_line)
```

### 匹配规则

- `prefix` / `suffix` 都按行内 substring 精确匹配
- 空字符串锚点只匹配空行
- 只在目标行号上下 3 行内搜索
- 先组合成 `[prefix_line, suffix_line]` 区间，再按方向、跨度、距离过滤和排序
- 替换整个行范围，不截取 prefix/suffix 字符位置
- `new_string` 行数不受限制；跨度校验只验证旧内容定位是否可信

## Data Model

### `FileReplaceInput` 变更

```python
class FileReplaceInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    start_no: int = Field(
        ge=1,
        description="Exact first line (1-based) to replace. Use the line number from the latest read output.",
    )
    end_no: int = Field(
        ge=1,
        description="Exact last line (1-based) to replace. Use the line number from the latest read output.",
    )
    prefix: str = Field(description="Substring expected on the first line to replace. Use an empty string only when the first line is empty.")
    suffix: str = Field(description="Substring expected on the last line to replace. Use an empty string only when the last line is empty.")
    new_string: str = Field(description="Replacement content. May contain any number of lines.")
```

### 新增内部辅助函数

```python
def _find_line_candidates(lines: list[str], target_line: int, snippet: str, radius: int = 3) -> list[int]:
    """在 target_line ± radius 内返回包含 snippet 的 1-based 行号。"""

def _rank_line_range_pairs(
    prefix_lines: list[int],
    suffix_lines: list[int],
    start_no: int,
    end_no: int,
) -> list[tuple[tuple[int, int, int, int], int, int]]:
    """返回按 score 排序后的 (score, prefix_line, suffix_line) 候选。"""
```

## API Contract

### 对外接口变更

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `start_no` | int | — | 起始行的准确行号，必须来自最新 read 输出 |
| `end_no` | int | — | 结束行的准确行号，必须来自最新 read 输出 |
| `prefix` | str | — | 起始行内 substring；空字符串仅表示起始行为空行 |
| `suffix` | str | — | 结束行内 substring；空字符串仅表示结束行为空行 |
| `new_string` | str | — | 替换内容，允许任意行数 |

### 内部函数变更

#### `_find_text_segment`（修改签名）

- **Before**: `_find_text_segment(lines, lineno, prefix, suffix)`
- **After**: `_find_text_segment(lines, start_no, end_no, prefix, suffix)`

#### `_execute_text_replace`（修改）

- 传入 `start_no` / `end_no`：从 `inp.start_no` / `inp.end_no` 取值

#### 错误信息增强

- prefix 未命中：
  ```
  prefix 'foo' not found within ±3 lines of start_no 42. Read the file to get current content.
  ```
- suffix 未命中：
  ```
  suffix 'bar' not found within ±3 lines of end_no 48. Read the file to get current content.
  ```
- 无合法 pair：
  ```
  no valid replace range found: candidate ranges did not match expected span 6 with less than 2 lines of drift.
  prefix candidates: 41, 43; suffix candidates: 44, 60.
  ```
- ambiguous：
  ```
  replace range is ambiguous: candidate ranges 41-47 and 43-49 have the same score. Provide more specific prefix/suffix or adjust start_no/end_no.
  ```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| prefix 在 `start_no ±3` 无候选 | 返回错误，提示重新读取文件 |
| suffix 在 `end_no ±3` 无候选 | 返回错误，提示重新读取文件 |
| 所有候选 pair 都方向错误或跨度差超过 2 | 返回错误，列出候选行和预期跨度 |
| 最小 score 有多个 pair | 返回 ambiguous，要求更具体的 prefix/suffix 或调整行号 |
| `prefix == suffix` | 无特殊字符级处理；按候选 pair 统一排序消歧 |
| `new_string` 行数与旧范围不同 | 允许；现有 diff/read coverage remap 负责处理 line shift |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 使用 `start_no` / `end_no` | `lineno` + `suffix_lineno` | 新语义是行范围替换，显式首尾行号更清晰 |
| 行级 substring 匹配 | 字符级 prefix/suffix offset | 消除 suffix 中途命中导致的残留文本 |
| 搜索半径固定为 ±3 行 | 保留 ±30 行 | 严格定位，避免在重复代码块里误匹配 |
| 先组合 pair 再评分 | prefix/suffix 分别取最近 | 整体区间更符合用户声明的 start/end 意图 |
| 旧范围跨度差必须小于 2 行 | 要求完全等于 `end_no - start_no` | 容忍轻微行号漂移，同时拒绝刚好卡在容忍边界的错误块 |
| 不限制 `new_string` 行数 | 要求新旧行数一致 | 定位严格即可；替换内容应支持扩行/删行 |
