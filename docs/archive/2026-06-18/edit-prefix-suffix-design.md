> **Status: Done**

# Edit 工具重构：prefix/suffix 段落匹配 — 技术设计文档

## Context

当前 edit 工具使用 `start_line` + `end_line` 精确行号定位，辅以 `anchor`（单行文本匹配修正）和 `scope`（搜索范围限定）。LLM 在多轮编辑后行号容易偏移，即使有 anchor 修正，仍需同时提供 `start_line`/`end_line` 两个行号，认知负担高。

核心问题：**行号是脆弱的定位方式**。LLM 更擅长描述"从 `def foo(` 到 `return bar` 之间的段落"，而不是"第 23 行到第 45 行"。

## Goals and Non-Goals

### Goals

- 用 `prefix` + `suffix` 替代 `start_line`/`end_line`/`anchor`/`scope`，以文本片段为定位基准
- `lineno` 必填，作为搜索起点 hint，不作为精确行号定位依据
- replace 操作：匹配 prefix~suffix 段落，替换为 newstr
- insert 操作：匹配 prefix~suffix 段落，在该段落后插入 newstr
- 保持 coverage tracking、staleness guard、file history 等机制不变
- 不保留向后兼容性

### Non-Goals

- 不改变 read/write 工具
- 不改变 file_state.py 的 coverage/staleness/remap 机制（仅适配调用方式）
- 不支持行内子串级别的替换（prefix/suffix 可匹配行内或跨行片段，但最终编辑仍按整行应用）
- 不改变 edit 的原子性（多 edit 从底到顶应用）和 diff 输出

## Architecture

### EditEntry 新模型

```
EditEntry (旧)                          EditEntry (新)
├── operation: replace | insert          ├── operation: replace | insert
├── start_line: int (1-based)           ├── lineno: int (搜索起点 hint，0 表示文件开头)
├── end_line: int | None                ├── prefix: str (段落起始片段)
├── new_string: str                     ├── suffix: str (段落结束片段)
├── scope: str | None  ← 删除           └── new_string: str
└── anchor: str | None  ← 删除
```

### 段落匹配算法

**核心函数**: `_find_paragraph(lines, operation, lineno, prefix, suffix) -> tuple[int, int] | str`

```
输入: 文件行列表, 操作类型, 搜索起点, 前缀片段, 后缀片段
输出: (start_line, end_line) 或错误字符串

算法:
1. lineno 必填；如果缺失 → 报错
2. 如果 lineno=0 且 prefix/suffix 都为空
   → 返回虚拟 BOF 零宽范围 `(0, 0)`
3. 如果 prefix 或 suffix 为空（非上述例外）→ 报错
4. 计算搜索窗口:
   - lineno=0 → `[1, min(total_lines, 100)]`
   - lineno>0 → `[max(1, lineno-100), min(total_lines, lineno+100)]`
5. 将窗口内容拼成文本，在窗口内收集所有 prefix 片段出现位置 → prefix_matches
   - prefix 可以是一行中的子串，也可以跨多行
   - 匹配只用于定位；替换/插入仍按整行范围执行
6. 如果 prefix_matches 为空 → 报未找到错误（提示调整 lineno）
7. 如果 prefix_matches 只有一个 → start_line 取 prefix 起始位置所在行
8. 如果 prefix_matches 有多个 → 以 lineno 为锚点选最近的:
   - 计算每个匹配起始行到 lineno 的距离；lineno=0 时按到文件开头的距离排序
   - 取距离最小的作为 start_line
   - 如果有多个等距匹配 → 报歧义错误，列出所有等距匹配行号
9. 从选中的 prefix 起始位置向后，找到第一个 suffix 片段 → end_line 取 suffix 结束位置所在行
   - 如果 prefix == suffix，suffix 可以匹配同一个片段，得到单行或同一片段覆盖的多行段落
   - 如果 suffix 未找到 → 报未找到错误
10. 返回 (start_line, end_line)

lineno 必填。搜索窗口为 lineno ±100 行；lineno=0 表示以文件开头作为 hint。
```

### 操作语义

**replace**:
- 匹配 prefix~suffix 段落 → 得到 (start_line, end_line)
- 将 [start_line, end_line] 替换为 new_string 的行
- 与旧 replace 语义一致，只是定位方式不同
- 如果匹配 BOF 零宽范围 `(0, 0)`，则在文件开头插入 new_string（等价 prepend）

**insert**:
- 匹配 prefix~suffix 段落 → 得到 (start_line, end_line)
- 在 end_line 之后插入 new_string 的行
- 如果匹配 BOF 零宽范围 `(0, 0)`，则在文件开头插入 new_string

### Coverage 检查适配

旧代码:
```python
range_end = edit.end_line if edit.operation == "replace" else edit.start_line
coverage_error = check_read_coverage(ctx, path, edit.start_line, range_end)
```

新代码（段落匹配后）:
```python
# _find_paragraph 返回 resolved (start, end)
# replace 和 insert 都检查整个段落的 coverage
if (start, end) != (0, 0):
    coverage_error = check_read_coverage(ctx, path, start, end)
```

insert 也需要检查整个段落的 coverage，因为插入位置依赖段落内容。
BOF 零宽范围不需要 coverage。

## Data Model

### EditEntry

```python
class EditEntry(BaseModel):
    operation: Literal["replace", "insert"] = Field(
        description=(
            "Edit operation. Use replace to replace a paragraph matched by "
            "prefix/suffix, or insert to add content after a matched paragraph."
        ),
    )
    lineno: int = Field(
        ge=0,
        description=(
            "Required search start hint. Use 1-based line numbers for normal edits; "
            "use 0 as a beginning-of-file hint. The tool searches within ±100 lines "
            "of this line for prefix/suffix matches. Not used as a precise target line."
        ),
    )
    prefix: str = Field(
        description=(
            "Text snippet that marks the beginning of the target paragraph. "
            "Can be a substring within a line or a short multi-line snippet. "
            "Must not be empty, except for beginning-of-file insertion/prepend (lineno=0)."
        ),
    )
    suffix: str = Field(
        description=(
            "Text snippet that marks the end of the target paragraph. "
            "Can be a substring within a line or a short multi-line snippet. "
            "For single-line targets, prefix and suffix can match the same line. "
            "Must not be empty, except for beginning-of-file insertion/prepend (lineno=0)."
        ),
    )
    new_string: str = Field(
        description=(
            "Replacement or inserted content. A trailing newline does not add "
            "an extra blank line; start with a newline only when an intentional "
            "blank first line is desired."
        ),
    )
```

### ResolvedEdit (替代旧的 EditEntry 在解析后的内部表示)

```python
class ResolvedEdit(NamedTuple):
    operation: Literal["replace", "insert"]
    start_line: int  # 段落起始行 (1-based；0 表示 BOF 零宽范围)
    end_line: int    # 段落结束行 (1-based；0 表示 BOF 零宽范围)
    new_string: str
```

### ParagraphResolution (替代 AnchorResolution)

```python
class ParagraphResolution(NamedTuple):
    edits: list[ResolvedEdit]
    hints: list[str]
```

## API Contract

### FileEditTool.execute

- **Input**: `FileEditInput(file_path, edits: list[EditEntry])`
- **Process**:
  1. resolve_safe + 存在性检查
  2. staleness 检查
  3. 读取文件
  4. 对每个 edit 调用 `_find_paragraph()` 解析段落范围
  5. 如果存在 insert 且 `new_string=""`，返回 no-op 提示，不写文件
  6. 验证：范围不重叠、insert 不在 replace 范围内
  7. Coverage 检查（BOF 零宽范围跳过）
  8. 从底到顶应用编辑；BOF 零宽范围统一在 `lines[0:0]` 插入
  9. 保存版本、写入、remap coverage、生成 diff
- **Output**: `ToolResult` (与现有格式一致)
- **Errors**:
  - lineno 缺失 → `"Edit {i}: lineno is required."`
  - prefix/suffix 为空 → `"Edit {i}: prefix and suffix must not be empty (except beginning-of-file insertion/prepend with lineno=0)."`
  - prefix 未找到 → `"Edit {i}: prefix {prefix!r} not found within ±100 lines of line {lineno}. Read the file to get current content."`
  - suffix 未找到 → `"Edit {i}: suffix {suffix!r} not found after prefix at line {start}. Read the file to get current content."`
  - prefix 歧义 → `"Edit {i}: prefix {prefix!r} is ambiguous at lines {lines}. Provide a more specific prefix or adjust lineno."`
  - 范围重叠 → `"Edit ranges must not overlap."`
  - coverage 不足 → `"Edit {i}: Lines {start}-{end} in {path} must be read before editing."`
  - insert 内容为空 → 不修改文件，返回提示 `"Edit {i}: insertion content is empty; no changes applied."`

### _find_paragraph

```python
def _find_paragraph(
    lines: list[str],
    operation: Literal["replace", "insert"],
    lineno: int,
    prefix: str,
    suffix: str,
) -> tuple[int, int] | str:
    """Find paragraph boundaries by prefix/suffix text matching.

    Returns (start_line, end_line) on success, or an error string.
    """
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| lineno 缺失 | 返回错误：lineno is required |
| prefix 或 suffix 为空（非 lineno=0 + prefix/suffix 都为空） | 返回错误：prefix/suffix must not be empty |
| prefix 在窗口内未找到 | 返回错误，提示 re-read 或调整 lineno |
| prefix 多个等距匹配 | 返回歧义错误，列出匹配行号，建议更精确的 prefix 或调整 lineno |
| suffix 未找到（prefix 之后） | 返回错误，提示 re-read |
| prefix == suffix 且匹配多行 | 以 lineno 选最近的 prefix 行，suffix 从该行开始搜索（含自身） |
| 范围重叠 | 返回错误，不应用任何编辑 |
| coverage 不足 | 返回错误，提示先 read |
| staleness | 返回错误，提示 re-read |
| insert 内容为空 | 返回 no-op 提示，不写文件，不标记 error |
| lineno=0 + prefix/suffix 为空 | 解析为文件开头 BOF 零宽范围；insert/replace 都表现为 prepend |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| prefix/suffix 片段匹配 | 仅行级包含匹配 | LLM 更容易给出小段稳定上下文；支持行内和跨行片段，但编辑仍按整行应用 |
| lineno 必填 | lineno 可选或完全删除 | 大文件中纯文本搜索效率低；必填 hint 让匹配窗口明确 |
| 搜索窗口 ±100 行 | ±200 或无限制全文件搜索 | 降低意外远距离匹配，鼓励 LLM 提供更准确的 lineno |
| suffix 取最近匹配 | 取最远匹配 | "最近"更符合直觉——段落通常是紧凑的 |
| insert 在段落后插入 | 在段落前插入 | 与旧 insert（在 start_line 后）语义对齐 |
| 不保留 anchor/scope | 保留并共存 | 用户明确要求不向后兼容；prefix/suffix 已完全覆盖 anchor+scope 的功能 |
| prefix/suffix 默认必填 | 可选 | 以文本内容为绝对定位基准，必须提供 |
| prefix/suffix 为空仅允许 lineno=0 | 完全禁止为空 | 需要支持文件开头 prepend；lineno=0 统一表示 BOF hint/虚拟零宽范围 |
| prefix 多匹配时以 lineno 选最近 | 报歧义错误 | lineno 是 hint 不是定位器，用它消歧比直接报错更实用 |

## 变更影响清单

### 需修改的文件

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/file_ops.py` | 重写 EditEntry、FileEditTool；删除 AnchorResolution/_resolve_anchored_edits/_scope_search_bounds/_find_text_lines 等；新增 _find_paragraph/ParagraphResolution/ResolvedEdit |
| `tests/test_tools/test_basic.py` | 重写所有 edit 相关测试用例 |

### 不需修改的文件

| 文件 | 原因 |
|------|------|
| `src/voidx/tools/base.py` | ToolContext/ToolResult 接口不变 |
| `src/voidx/tools/file_state.py` | coverage/staleness/remap 机制不变 |
| `src/voidx/permission/*` | 只关心 tool name "edit"，不关心参数 |
| `src/voidx/agent/graph/runtime_guards.py` | 只检查 tool name |
| `src/voidx/workflow/nodes.py` | 只在 denied_tools 列表中引用 "edit" |
| `src/voidx/ui/*` | 只关心 tool name 和 diff 输出 |

### 需更新的描述

| 位置 | 变更 |
|------|------|
| FileWriteTool description | 更新 "anchor placeholders" → "prefix/suffix markers" |
| FileEditTool description | 完全重写 |

## 已确定的设计细节

### prefix/suffix 为空的语义

- prefix 和 suffix **默认不允许为空**
- 唯一例外：`lineno=0` 且 `prefix=""` + `suffix=""`，表示匹配文件开头的 BOF 零宽范围 `(0, 0)`
- 对这个 BOF 零宽范围，`insert` 和 `replace` 都表现为 prepend；推荐调用方使用 `insert` 表达意图
- 其他任何情况下 prefix 或 suffix 为空 → 报错：`"Edit {i}: prefix and suffix must not be empty (except beginning-of-file insertion/prepend with lineno=0)."`

### insert 在文件开头

- `lineno=0` + `prefix=""` + `suffix=""` → 在文件第 1 行之前插入
- 此时跳过段落匹配，直接在文件开头插入 new_string
- coverage 检查也跳过（文件开头不需要 read coverage）

### 搜索窗口上限

- 以 lineno 为中心，搜索窗口为 **±100 行**（即搜索范围 `[max(1, lineno-100), min(total_lines, lineno+100)]`）
- lineno 必填；lineno=0 时搜索范围为 `[1, min(total_lines, 100)]`
- 如果 prefix 在窗口内未找到 → 报错，提示调整 lineno 或扩大搜索范围
- 窗口外可能存在匹配，但不搜索——这鼓励 LLM 提供更准确的 lineno hint

### prefix == suffix 时的 suffix 搜索

当 prefix 和 suffix 相同时，从选中的 prefix 起始位置开始搜索 suffix（含同一片段）。
如果同一片段同时满足 prefix/suffix → end_line 取该片段结束位置所在行。

### prefix/suffix 长度

prefix/suffix 可以是一小段文本，也可以更长；推荐使用能唯一定位目标的最短稳定片段。
过长片段更容易因为无关改动失效，通常收益不大。

### lineno=0 的语义

lineno=0 是统一的文件开头 hint：

- `lineno=0` + 非空 prefix/suffix → 在文件前 100 行内搜索片段
- `lineno=0` + `prefix=""` + `suffix=""` → 匹配 BOF 零宽范围 `(0, 0)`
- 对 BOF 零宽范围执行 `insert` 或 `replace` 都会在文件开头 prepend；推荐使用 `insert`
- lineno 不能省略

### insert 内容为空

insert 的 `new_string=""` 不报错；工具返回 no-op 提示，不保存版本、不写文件、不生成 diff。
replace 的 `new_string=""` 仍表示删除匹配段落。

## 示例

### replace 示例

文件内容:
```python
def foo():
    x = 1
    return x

def bar():
    y = 2
    return y
```

编辑:
```json
{
  "operation": "replace",
  "lineno": 5,
  "prefix": "def bar():",
  "suffix": "return y",
  "new_string": "def bar():\n    y = 3\n    return y"
}
```

结果: 第 5-7 行被替换，`y = 2` 变为 `y = 3`。

### insert 示例

文件内容同上。

编辑:
```json
{
  "operation": "insert",
  "lineno": 3,
  "prefix": "def foo():",
  "suffix": "return x",
  "new_string": "\ndef baz():\n    z = 4\n    return z"
}
```

结果: 在第 3 行（`return x`）之后插入 `def baz()` 函数。

### prefix 歧义示例

文件内容:
```python
def foo():
    x = 1
    return x

def foo():
    x = 2
    return x
```

编辑:
```json
{
  "operation": "replace",
  "lineno": 3,
  "prefix": "def foo():",
  "suffix": "return x",
  "new_string": "def foo():\n    x = 99\n    return x"
}
```

lineno=3 时，两个 `def foo():` 分别在第 1 行和第 5 行，距离相等，报歧义错误。
需要更精确的 prefix 如 `def foo():\n    x = 1` 或调整 lineno。

### 文件开头 insert 示例

编辑:
```json
{
  "operation": "insert",
  "lineno": 0,
  "prefix": "",
  "suffix": "",
  "new_string": "# header\n"
}
```

结果: 在文件第 1 行之前插入 `# header`。
