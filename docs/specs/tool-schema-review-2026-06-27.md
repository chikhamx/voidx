# Tool Schema & Prompt Description Review

> **Status: In Progress**
> **Date: 2026-06-27**
> **Scope: 全量审查工具 schema、字段描述、系统提示词、route hint、工具出错提示**

## 1. 审查范围

| 类别 | 文件 |
|------|------|
| 文件操作 | `file_ops/read.py`, `file_ops/file.py`, `file_ops/write.py`, `file_ops/edit_execute.py`, `file_ops/edit_resolve.py` |
| 搜索 | `search.py` (GlobTool, GrepTool) |
| Shell | `bash/tool.py`, `bash/core.py`, `bash/router.py`, `bash/safety.py`, `bash/hint/*.py` |
| Git | `git.py` |
| LSP | `lsp.py` |
| 工作流 | `workflow.py`, `plan_checkpoint.py`, `compact_context.py` |
| Agent | `agent.py` |
| Web | `webfetch.py`, `websearch.py` |
| 其他 | `clarify.py`, `todo.py`, `skills.py`, `task_status.py`, `load_doc_template.py` |
| 系统提示词 | `agent/prompts.py` |
| 基础设施 | `tools/base.py`, `tools/registry.py`, `tools/file_state.py` |

## 2. 工具 Schema & 字段描述问题

### 2.1 🔴 精准性问题（描述与行为不一致）

#### P1: `write` — `new_string` 字段描述包含 runtime 实现细节

- **文件**: `src/voidx/tools/file_ops/write.py:24-27`
- **当前**:
  ```python
  new_string: str = Field(
      default="",
      description="For insert and append: content to add. A trailing newline does not add an extra blank line.",
  )
  ```
- **问题**: "A trailing newline does not add an extra blank line" 是 runtime 行为说明，不应出现在字段描述中。LLM 只需知道"传什么内容"，尾部换行如何处理是 runtime 的事。
- **修复**:
  ```python
  new_string: str = Field(
      default="",
      description="For insert and append: content to add.",
  )
  ```

#### P2: `file` — `dest_path` 未说明在非 move 操作时被忽略

- **文件**: `src/voidx/tools/file_ops/file.py:28-31`
- **当前**: `"Destination path for move operation. Required when op=move."`
- **问题**: LLM 在 `op=create/delete` 时可能误传 `dest_path`。`workflow` 工具的 `condition` 字段已采用 "Ignored for 'enter' and 'done'." 的写法，应保持一致。
- **修复**: `"Destination path for move operation. Required when op=move; ignored for create and delete."`

#### P3: `write` — `lineno` 未说明在 append 时被忽略

- **文件**: `src/voidx/tools/file_ops/write.py:20-23`
- **当前**: `"For insert: 0-based line number to insert before."`
- **修复**: `"For insert: 0-based line number to insert before. Ignored for append."`

#### P4: `lsp` — `line`/`character` 描述说 "Required" 但有默认值

- **文件**: `src/voidx/tools/lsp.py:30-40`
- **当前**:
  ```python
  line: int = Field(default=1, ge=1, description="1-based line number. Required for definition, references.")
  character: int = Field(default=0, ge=0, description="0-based character offset. Required for definition, references.")
  ```
- **问题**: 字段有默认值，JSON Schema 不会标记为 required（`model_to_json_schema` 将所有字段放入 required 数组，但 LLM 仍可能依赖描述文本判断是否必须传值）。描述说 "Required" 但实际有默认值且运行时未强制校验。
- **修复**:
  ```python
  line: int = Field(default=1, ge=1, description="1-based line number. Must be set for definition and references.")
  character: int = Field(default=0, ge=0, description="0-based character offset. Must be set for definition and references.")
  ```

### 2.2 🟡 简洁性问题（描述冗余）

#### P5: `grep` — `whole_word` 描述混入使用指导

- **文件**: `src/voidx/tools/search.py:115`
- **当前**: `"Match whole words only (adds \\b boundaries). Do not add \\b to pattern when using this."`
- **问题**: 后半句是使用指导，混入了字段描述。
- **修复**: `"Match whole words only by adding word boundaries."`

#### P6: `agent` — description 过长，混入使用策略

- **文件**: `src/voidx/tools/agent.py:129-139`
- **当前**: 约 80 词，包含使用限制、排除场景、参数要求、上下文说明。
- **问题**: 工具描述应聚焦"做什么"，使用策略（"Use ONLY when..."、"Do not use for..."）更适合放在系统提示词中。
- **修复**: 精简为:
  ```python
  description = (
      "Start an isolated child agent for a delegated task. The child receives "
      "the task brief and runtime context, but not caller conversation history."
  )
  ```
  使用限制部分已在系统提示词的 global_rules 中覆盖（"Pick the smallest next action"）。

#### P7: `skill` — description 过长

- **文件**: `src/voidx/tools/skills.py:54-60`
- **当前**: 约 55 词。
- **修复**: `"Load skill instructions, create a new SKILL.md, or list discovered skills. Load/list are read-only; create writes a SKILL.md file."`

### 2.3 🟢 一致性问题

#### P8: `file_path` 字段描述不统一

| 工具 | 当前描述 | 建议 |
|------|----------|------|
| `read` | `"Absolute or relative path to the file"` | ✅ 保持 |
| `lsp` | `"Absolute or relative path to the file..."` | ✅ 保持 |
| `file` | `"Path to the file"` | → `"Absolute or relative path to the file"` |
| `write` | `"Path to the file"` | → `"Absolute or relative path to the file"` |
| `replace` | `"Path to edit"` | → `"Absolute or relative path to the file"` |

#### P9: `git` — `path` 字段未说明可接受绝对路径

- **文件**: `src/voidx/tools/git.py:66`
- **当前**: `"Optional execution path relative to workspace. Empty uses workspace root."`
- **问题**: `resolve_safe` 实际支持绝对路径，但描述未提及。
- **修复**: `"Optional path relative to workspace (or absolute). Empty uses workspace root."`

## 3. 系统提示词审查

### 3.1 整体评价

`agent/prompts.py` 中的 `BASE_SYSTEM` 质量较高，规则简洁、具体、可操作。以下为细节问题。

### 3.2 问题

#### P10: `global_rules` 中缺少 agent 工具使用策略

- **文件**: `src/voidx/agent/prompts.py:107-131`
- **问题**: P6 建议将 agent 工具的使用策略从工具描述移至系统提示词，但当前 `global_rules` 中没有关于子代理委派的规则。如果精简 agent description，需要在此补充。
- **修复**: 在 `global_rules` 中添加:
  ```python
  PromptRule(
      detail="Delegate to child agents only for parallel independent tasks or when the user explicitly asks. Do not delegate single-file reads, simple searches, or straightforward tasks you can do directly.",
  ),
  ```

#### P11: `communication_style` 中 "Show progress via todo" 规则与工具描述重叠

- **文件**: `src/voidx/agent/prompts.py:102-105`
- **当前**: `"Update the todo list so progress is visible. But don't narrate todo updates in your text."`
- **问题**: 这条规则本身没问题，但 `todo` 工具的 description 重复了 "Use semantic string ids" 等使用指导。工具描述应聚焦功能，使用策略由系统提示词驱动。
- **建议**: 保持当前系统提示词不变，但 todo 工具 description 可精简（见 P12）。

#### P12: `todo` 工具 description 包含使用指导

- **文件**: `src/voidx/tools/todo.py:58-65`
- **当前**: 包含 "Use semantic string ids (e.g., 'schema', 'api') for easy reference. Status: pending → active → done."
- **问题**: 使用指导混入工具描述。
- **修复**: 精简为 `"Create and manage a task list. Supports write (full replace), update (incremental by id), and read (query with filter)."`，使用指导保留在字段描述中（`id` 字段已有 "Semantic id" 描述）。

## 4. Route Hint 审查

### 4.1 整体评价

`bash/hint/` 下的 route hint 质量很高，覆盖了 cat/head/tail → read、echo/heredoc → write/file、find → glob、grep/rg → grep、sed → replace、git → git。hint 的 `llm_hint` 字段会作为 `next_step_hint` 返回给 LLM。

### 4.2 问题

#### P13: sed hint 中 `line()` 工具名已不存在

- **文件**: `src/voidx/tools/bash/hint/file.py:135, 183`
- **当前**:
  ```python
  llm_hint=f'Prefer file(file_path="{path}", op="create") then line(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
  ```
- **问题**: `line` 工具不存在，应为 `write`。这是历史遗留——早期可能有 `line` 工具，后来合并到 `write`。
- **修复**: 将 `line(...)` 改为 `write(...)`:
  ```python
  llm_hint=f'Prefer file(file_path="{path}", op="create") then write(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
  ```

#### P14: sed hint 的 `llm_hint` 过长且包含实现细节

- **文件**: `src/voidx/tools/bash/hint/search.py:236, 241, 245`
- **当前**:
  ```python
  llm_hint=f'Prefer replace(...) — prefix/suffix are line content anchors for locating the edit, new_string is the replacement. Enables staleness checking and diff output.'
  ```
- **问题**: "prefix/suffix are line content anchors for locating the edit" 是实现细节，LLM 不需要理解内部机制就能调用 replace 工具。"Enables staleness checking and diff output" 也是实现细节。
- **修复**: 简化为 `'Prefer replace(file_path="{path}", start_no={line_no}, end_no={line_no}, prefix="{old_text}", suffix="{old_text}", new_string="{new_text}").'`

#### P15: sed pattern delete hint 引导不够具体

- **文件**: `src/voidx/tools/bash/hint/search.py:267-269`
- **当前**: `'For pattern-based deletion: first grep "{pat}" {path} to locate lines, then use replace(..., new_string="").'`
- **问题**: `replace(...)` 缺少关键参数说明，LLM 可能不知道需要 start_no/end_no/prefix/suffix。
- **修复**: `'For pattern-based deletion: first grep "{pat}" {path} to locate matching lines, then use replace(file_path="{path}", start_no, end_no, prefix, suffix, new_string="") with the matched line numbers and content.'`

## 5. 工具出错提示审查

### 5.1 整体评价

工具出错提示整体质量较好，大部分错误消息清晰、可操作。以下为发现的问题。

### 5.2 问题

#### P16: `file_state.py` — read coverage 错误消息使用 resolved path 而非 display path

- **文件**: `src/voidx/tools/file_state.py:225, 231`
- **当前**:
  ```python
  return f"Lines {start_line}-{end_line} in {resolved} must be read before editing."
  ```
- **问题**: `resolved` 是绝对路径（如 `/Users/chikham/workspace/voidx/src/voidx/tools/file_ops/file.py`），而 LLM 传入的是相对路径。错误消息应使用 LLM 传入的 display path，与其他错误消息保持一致。
- **影响**: LLM 可能难以将错误消息中的绝对路径与它传入的相对路径关联起来。
- **修复**: 需要将 display path 传入 `check_read_coverage`，或在上层捕获后替换路径。这是一个较大的改动，建议作为后续优化。

#### P17: `git.py` — `command_denied` 错误消息过于简略

- **文件**: `src/voidx/tools/git.py:111, 115`
- **当前**: `error="command_denied"`
- **问题**: LLM 收到 `"error": "command_denied"` 后不知道为什么被拒绝。应说明被拒绝的原因（如 "destructive subcommand" 或 "destructive flag"）。
- **修复**:
  ```python
  # line 111
  return _result(subcommand, ctx, repo=repo, ok=False, error=f"command_denied: subcommand '{subcommand}' is destructive and not allowed")
  # line 115
  return _result(subcommand, ctx, repo=repo, ok=False, error=f"command_denied: destructive flag in '{subcommand}'")
  ```

#### P18: `read.py` — 外部文件读取交互提示使用中文

- **文件**: `src/voidx/tools/file_ops/read.py:191-192`
- **当前**:
  ```python
  prompt=f"读取 workspace 外的文件: {inp.file_path}",
  options=[("允许", "allow", "本次允许读取该文件"), ("拒绝", "deny", "不读取该文件")],
  ```
- **问题**: 这是面向用户的交互提示，使用中文是合理的（项目语言偏好为中文）。但如果未来支持英文用户，应考虑国际化。当前保持不变。
- **建议**: 保持现状，记录为已知设计决策。

#### P19: `todo.py` — 错误消息前缀不一致

- **文件**: `src/voidx/tools/todo.py:146, 226, 242`
- **当前**: 部分错误消息以 `"Error: "` 开头（如 `"Error: 'updates' is required for update operation."`），部分不以 "Error" 开头（如 `"Todo tracker is not available in this runtime."`）。
- **问题**: 错误消息格式不一致。其他工具（如 file、git）的错误消息不以 "Error: " 开头，而是直接描述问题。
- **修复**: 移除 `"Error: "` 前缀，统一为 `"'updates' is required for update operation."` 和 `"Duplicate ids found: ..."`。

#### P20: `skills.py` — 成功消息混用中英文

- **文件**: `src/voidx/tools/skills.py:159-161`
- **当前**:
  ```python
  output=(
      f"Created skill '{name}' at {path}. "
      f"使用时在对话中输入 #{name} 引用该 skill，"
      f"或手动编辑该文件添加 triggers 字段以启用自动触发。"
  ),
  ```
- **问题**: 成功消息混用英文和中文。应统一为英文（工具输出面向 LLM，LLM 再决定如何向用户呈现）。
- **修复**:
  ```python
  output=(
      f"Created skill '{name}' at {path}. "
      f"Reference it with #{name} in conversation, "
      f"or edit the file to add a triggers field for auto-activation."
  ),
  ```

## 6. 修复方案汇总

### 6.1 优先级分类

| 优先级 | ID | 问题 | 文件 | 影响 |
|--------|----|----|------|------|
| 🔴 高 | P13 | sed hint 引用不存在的 `line` 工具 | `bash/hint/file.py` | LLM 调用不存在的工具 |
| 🔴 高 | P1 | `write` new_string 描述含 runtime 细节 | `file_ops/write.py` | 描述歧义 |
| 🔴 高 | P4 | `lsp` line/character "Required" 与默认值矛盾 | `lsp.py` | LLM 困惑 |
| 🟡 中 | P2 | `file` dest_path 未说明忽略场景 | `file_ops/file.py` | LLM 误传参数 |
| 🟡 中 | P3 | `write` lineno 未说明忽略场景 | `file_ops/write.py` | LLM 误传参数 |
| 🟡 中 | P17 | `git` command_denied 过于简略 | `git.py` | LLM 不知原因 |
| 🟡 中 | P20 | `skills` 成功消息混用中英文 | `skills.py` | 输出不一致 |
| 🟡 中 | P19 | `todo` 错误消息前缀不一致 | `todo.py` | 一致性 |
| 🟢 低 | P5 | `grep` whole_word 描述冗余 | `search.py` | 可读性 |
| 🟢 低 | P6 | `agent` description 过长 | `agent.py` | 可读性 |
| 🟢 低 | P7 | `skill` description 过长 | `skills.py` | 可读性 |
| 🟢 低 | P8 | `file_path` 描述不统一 | 多个文件 | 一致性 |
| 🟢 低 | P9 | `git` path 未说明绝对路径 | `git.py` | 边界情况 |
| 🟢 低 | P10 | 系统提示词缺少 agent 委派规则 | `prompts.py` | 策略完整性 |
| 🟢 低 | P12 | `todo` description 含使用指导 | `todo.py` | 可读性 |
| 🟢 低 | P14 | sed hint llm_hint 过长 | `bash/hint/search.py` | 可读性 |
| 🟢 低 | P15 | sed pattern delete hint 不够具体 | `bash/hint/search.py` | 可操作性 |
| 📋 后续 | P16 | read coverage 错误用 resolved path | `file_state.py` | 改动较大 |
| 📋 后续 | P11 | todo 系统提示词与工具描述重叠 | `prompts.py` | 设计决策 |
| 📋 后续 | P18 | 外部文件读取交互提示中文 | `read.py` | 设计决策 |

### 6.2 实施计划

#### 阶段一：高优先级修复（P1, P4, P13）

1. **P1**: 修改 `write.py` 的 `new_string` 字段描述
2. **P4**: 修改 `lsp.py` 的 `line`/`character` 字段描述
3. **P13**: 修改 `bash/hint/file.py` 中 `line()` → `write()`

#### 阶段二：中优先级修复（P2, P3, P17, P19, P20）

4. **P2**: 修改 `file.py` 的 `dest_path` 字段描述
5. **P3**: 修改 `write.py` 的 `lineno` 字段描述
6. **P17**: 修改 `git.py` 的 `command_denied` 错误消息
7. **P19**: 统一 `todo.py` 错误消息格式
8. **P20**: 统一 `skills.py` 成功消息为英文

#### 阶段三：低优先级修复（P5, P6, P7, P8, P9, P10, P12, P14, P15）

9. **P5**: 精简 `grep` 的 `whole_word` 描述
10. **P6**: 精简 `agent` description
11. **P7**: 精简 `skill` description
12. **P8**: 统一 `file_path` 字段描述
13. **P9**: 补充 `git` path 描述
14. **P10**: 在系统提示词中补充 agent 委派规则
15. **P12**: 精简 `todo` description
16. **P14**: 精简 sed hint
17. **P15**: 补充 sed pattern delete hint

#### 阶段四：后续优化（P16, P11, P18）

18. **P16**: read coverage 错误消息使用 display path（需重构 `check_read_coverage` 签名）
19. **P11/P18**: 设计决策，保持现状

### 6.3 验证方式

- 修改后运行 `.venv/bin/python -m pytest tests/test_tools/ -v` 确保工具测试通过
- 对涉及 route hint 的修改，运行 `.venv/bin/python -m pytest tests/ -k "hint" -v`
- 对涉及 schema 的修改，检查 `model_to_json_schema` 输出是否正确
