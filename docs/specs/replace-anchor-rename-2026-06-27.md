# replace 工具 prefix/suffix 参数改名 — 技术设计文档

## Context

`replace` 工具的 `prefix` 和 `suffix` 参数实际语义是"行内容锚点"——在 `start_no`/`end_no` 附近 ±3 行内搜索包含该子串的行，用来定位实际替换的起始/结束行。它们**不是**字符串前缀/后缀（不使用 `startswith`/`endswith`），而是行内任意位置的子串匹配。

内部代码已使用准确的术语：`_line_matches_replace_anchor`（`edit_resolve.py:161`）、`anchor`（`edit_resolve.py:118`）。但对外暴露的参数名 `prefix`/`suffix` 与实际语义不符，在字符串处理语境下容易误导 LLM 和开发者理解为"行首/行尾子串"。

同时，前序审查文档（`tool-schema-review-2026-06-27.md`）中 P14 指出 sed route hint 中 `prefix/suffix are line content anchors` 的说明过长且包含实现细节。改名后该说明可自然简化。

## Goals and Non-Goals

### Goals

- 将 `FileReplaceInput` 的 `prefix`→`start_anchor`、`suffix`→`end_anchor`，与内部 `anchor` 术语统一
- 同步更新所有源码、route hint、测试中的参数名引用
- 借此机会修复审查文档中 P14（sed hint 过长）的相关问题
- 保持所有测试绿色

### Non-Goals

- 不改变 `ResolvedEdit` 内部类型（它 resolve 后不保留 anchor 信息）
- 不处理审查文档中的其他问题（P1-P20 中的非关联项，包括 P8 file_path 描述统一——范围过大，单独处理）
- 不做向后兼容（不同时接受 `prefix` 和 `start_anchor`）——voidx 处于快速迭代期，session transcript 中的旧参数名调用会在下次工具执行时自然失败并提示 LLM 重新调用

## Architecture

### 影响面分层

```
Layer 1: 公开 API 契约（必须改）
├── FileReplaceInput schema (edit_execute.py:43-56)
│   ├── prefix → start_anchor
│   └── suffix → end_anchor
├── FileReplaceTool.description (edit_execute.py:74-79)
│   └── "prefix/suffix substrings" → "start_anchor/end_anchor"
└── _execute_text_replace 签名 (edit_execute.py:114-124)
    ├── prefix → start_anchor
    └── suffix → end_anchor

Layer 2: Route Hint（必须改）
└── bash/hint/search.py:236,241,245,253,261
    └── llm_hint 字符串中的 prefix=/suffix= → start_anchor=/end_anchor=
    └── 移除 "prefix/suffix are line content anchors" 说明（改名后自解释）

Layer 3: 内部实现（不改，见 Non-Goals）
├── edit_resolve.py: _find_text_segment 参数名 — 保持 prefix/suffix
├── edit_resolve.py: _find_single_line_segment 参数名 — 保持 prefix/suffix
├── edit_resolve.py: 局部变量 prefix_lines/suffix_lines — 保持
└── edit_resolve.py: 错误消息中的 "prefix"/"suffix" 措辞 — 需改（面向 LLM）
    注：内部函数参数名不改（私有实现，不影响 API 契约），
    但错误消息会返回给 LLM，需与参数名一致。

Layer 4: 测试（必须改）
├── 直接构造 FileReplaceInput 的测试
├── 用 dict 构造 replace 工具调用的测试
├── 断言 schema properties 的测试
└── 断言 route hint 文本的测试
```

### 不受影响的部分

- **Agent 集成测试中的旧格式 mock**：`tests/test_agent/` 中约 21 处使用 `{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old"}` 格式构造的 mock 数据。这些使用的是已不存在的 `"edits"` 批量编辑接口格式，测试只验证工具调用序列不实际执行 replace，且不断言 `prefix`/`suffix` 参数名。**这些不需要改**——它们是历史遗留 mock，与当前 `FileReplaceInput` schema 无关。
- **死代码辅助函数**：`tests/test_tools/` 中 5 个文件定义了 `_replace()`/`_insert()` 辅助函数但从未调用。**不需要改**——它们是死代码，后续可单独清理。

## Data Model

### 改名前后对比

```
FileReplaceInput (改前)
├── file_path: str
├── start_no: int (ge=1)
├── end_no: int (ge=1)
├── prefix: str          ← 改名
├── suffix: str          ← 改名
└── new_string: str

FileReplaceInput (改后)
├── file_path: str
├── start_no: int (ge=1)
├── end_no: int (ge=1)
├── start_anchor: str    ← 新名
├── end_anchor: str      ← 新名
└── new_string: str
```

### 字段描述改进

改前：
```python
prefix: str = Field(
    description=(
        "Substring expected anywhere on the first line to replace. "
        "Use an empty string only when the first line is empty. "
        "Aim for a distinctive snippet."
    ),
)
suffix: str = Field(
    description=(
        "Substring expected anywhere on the last line to replace. "
        "Use an empty string only when the last line is empty. "
        "Aim for a distinctive snippet."
    ),
)
```

改后：
```python
start_anchor: str = Field(
    description=(
        "Content anchor on the first line to replace — a substring "
        "expected anywhere on that line. Use an empty string only "
        "when the first line is empty. Aim for a distinctive snippet."
    ),
)
end_anchor: str = Field(
    description=(
        "Content anchor on the last line to replace — a substring "
        "expected anywhere on that line. Use an empty string only "
        "when the last line is empty. Aim for a distinctive snippet."
    ),
)
```

### 工具描述改进

改前：
```python
description = (
    "Replace whole lines in a file. "
    "Provide the exact start_no/end_no from the latest read output, "
    "plus prefix/suffix substrings from the first and last lines. "
    "Read the target lines first."
)
```

改后：
```python
description = (
    "Replace whole lines in a file. "
    "Provide the exact start_no/end_no from the latest read output, "
    "plus start_anchor/end_anchor substrings from the first and last lines. "
    "Read the target lines first."
)
```

## API Contract

### FileReplaceInput

- **Signature**: `FileReplaceInput(file_path: str, start_no: int, end_no: int, start_anchor: str, end_anchor: str, new_string: str)`
- **JSON Schema properties**: `{"file_path", "start_no", "end_no", "start_anchor", "end_anchor", "new_string"}`
- **所有字段仍为 required**（`model_to_json_schema` 将所有 properties 放入 required 数组，`base.py:175`）

### _execute_text_replace

- **Signature**: `_execute_text_replace(ctx, *, file_path, start_no, end_no, start_anchor, end_anchor, new_string, tool_name)`
- **调用 `_find_text_segment`**: 传递 `start_anchor`/`end_anchor`，内部参数名可同步改或保持 `prefix`/`suffix`（私有函数，不影响 API）

### Route Hint 输出

改前（`search.py:236`）：
```python
llm_hint=f'Prefer replace(file_path="{path}", start_no={line_no}, end_no={line_no}, prefix="{old_text}", suffix="{old_text}", new_string="{new_text}") — prefix/suffix are line content anchors for locating the edit, new_string is the replacement. Enables staleness checking and diff output.'
```

改后：
```python
llm_hint=f'Prefer replace(file_path="{path}", start_no={line_no}, end_no={line_no}, start_anchor="{old_text}", end_anchor="{old_text}", new_string="{new_text}").'
```

> 改名后 `start_anchor`/`end_anchor` 语义自解释，移除 "are line content anchors" 的冗余说明和 "Enables staleness checking and diff output" 的实现细节。这同时修复了审查文档 P14。

## Error Handling

### 内部错误消息

`edit_resolve.py` 中的错误消息目前使用 "prefix"/"suffix" 措辞：

```python
# edit_resolve.py:52
prefix_target = "empty line" if prefix == "" else f"prefix {prefix!r}"
# edit_resolve.py:61
suffix_target = "empty line" if suffix == "" else f"suffix {suffix!r}"
# edit_resolve.py:85
"Provide more specific prefix/suffix or adjust start_no/end_no."
# edit_resolve.py:122
f"prefix {prefix!r} found but suffix {suffix!r} not on the same line ..."
# edit_resolve.py:135
"Provide a more specific prefix/suffix."
```

改为使用 "start_anchor"/"end_anchor" 措辞，保持错误消息与参数名一致。

| 失败场景 | 处理策略 |
|---------|---------|
| LLM 传入旧参数名 `prefix`/`suffix` | Pydantic 校验失败，返回 `Invalid arguments` 错误。LLM 会根据错误重新调用。不做兼容。 |
| Session transcript 中有旧格式工具调用 | 同上，重新执行时会失败。历史记录只读，不影响。 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 参数名用 `start_anchor`/`end_anchor` | `prefix`/`suffix`（保持不变） | 改名后语义自解释，与内部 `anchor` 术语一致，消除"字符串前缀/后缀"的误导 |
| 参数名用 `start_anchor`/`end_anchor` | `start_snippet`/`end_snippet` | `anchor` 已是内部使用的术语（`_line_matches_replace_anchor`），保持一致 |
| 参数名用 `start_anchor`/`end_anchor` | `first_anchor`/`last_anchor` | `start`/`end` 与 `start_no`/`end_no` 形成对称，LLM 更容易关联 |
| 不做向后兼容 | 同时接受 `prefix` 和 `start_anchor` | voidx 快速迭代期，兼容层增加复杂度且无长期价值 |
| 内部函数参数名不改 | 同步改内部参数名 | 私有实现不影响 API 契约；内外不一致是小代价，但避免过度扩散改动面。错误消息需改因为面向 LLM。 |
| 移除 route hint 中的 "are line content anchors" 说明 | 保留说明 | 改名后 `start_anchor`/`end_anchor` 已自解释，说明变冗余 |

## Open Questions

- [ ] `edit_resolve.py` 错误消息中 `prefix`/`suffix` 措辞改为 `start_anchor`/`end_anchor` 后，消息会变长（如 `start_anchor {val!r}` vs `prefix {val!r}`）。是否可接受？倾向接受——准确性优先于简洁。
- [ ] `test_bash_router.py:39,44` 断言 `"prefix/suffix are line content anchors" in h.llm_hint`。改名后 hint 文本不再包含此短语，需同步更新断言。确认无其他测试断言旧 hint 文本。
