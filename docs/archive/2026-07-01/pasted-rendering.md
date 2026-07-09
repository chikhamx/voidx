> **Status: Done**

# 粘贴内容富文本渲染 — 技术设计文档

## Context

用户在 TUI 中粘贴多行文本后提交，粘贴内容会被 `<pasted>\n...\n</pasted>` 标签包裹，发送给 LLM。这个标签对 LLM 有语义价值（区分用户手打内容和粘贴内容），但在 UI 渲染时会原样显示 `<pasted>` 和 `</pasted>` 文本，体验差。

**当前数据流：**

```
TUI 粘贴 → parser.py 注册 [Pasted text #N] token
         → input.py:84 提交时 _expand_registered_tokens 展开为 <pasted>\n{内容}\n</pasted>
         → turn_runner.py:124 TurnStarted(text=display_text) 回显
         → dock/app.py:215 start_turn() escape() 后原样显示 <pasted> 标签
         → gateway/adapter.py:422 turn.started 事件转发到 web
```

**问题点：**
- TUI 端 `start_turn()`（`dock/app.py:215`）对文本做 `escape()`，`<pasted>` 标签原样显示
- Web 端 `main.js:331` 用户消息用 `style: "text"` → `pre.textContent`，纯文本不渲染

**设计原则：** 生成层不变（`<pasted>` 标签仍发给 LLM），只在渲染层转换。

## Goals and Non-Goals

### Goals

- TUI 端回显用户消息时，`<pasted>` 块内容渲染为 Markdown 富文本，不显示标签文本
- Web 端回显用户消息时，`<pasted>` 块内容渲染为 Markdown 富文本，不显示标签文本
- 保留粘贴内容的视觉区分（与手打文本有区别），便于用户识别
- `<pasted>` 标签仍正常发送给 LLM，不影响模型行为

### Non-Goals

- 不修改 `<pasted>` 标签的生成逻辑（`input.py:84`）
- 不修改 web 端输入框的粘贴处理（web 端无 paste token 机制）
- 不处理用户手打 `<pasted>` 文本的边缘情况（概率极低，不在本期范围）

## Architecture

两端策略不同：

**Web 端**（统一预处理）：将 `<pasted>` 块转为 Markdown blockquote，整条消息统一走 `renderMarkdown`。

```
用户消息文本 (含 <pasted> 标签)
    │
    ▼
stripPastedTags(text)  ← JS 预处理函数
    │  提取 <pasted>...</pasted> 块
    │  转换为 Markdown blockquote (每行加 "> " 前缀)
    │  非粘贴部分保持原样
    ▼
renderMarkdown(result)  ← 整条消息统一 Markdown 渲染
```

**TUI 端**（分段渲染）：不统一预处理，而是按 `<pasted>` 块拆分，分段渲染。原因：TUI 的 `start_turn` 当前用 `escape()` 纯文本显示用户消息，若整条消息走 Markdown 会误渲染手打的 `*` `_` `#` 等字符。

```
用户消息文本 (含 <pasted> 标签)
    │
    ▼
split_pasted_segments(text)  ← 分段函数
    │  返回 [(is_pasted: bool, content: str), ...]
    ▼
逐段渲染:
    │  非 pasted 段 → escape() 纯文本 (现有行为)
    │  pasted 段    → _markdown_lines() Markdown 渲染
    ▼
合并为 turn node 的 header + body_lines
```

### 模块边界

| 层级 | 文件 | 职责 |
|------|------|------|
| TUI 分段 | `src/voidx/ui/output/dock/formatting.py` | `split_pasted_segments()` — 将文本按 `<pasted>` 块拆分为段列表 |
| TUI 渲染 | `src/voidx/ui/output/dock/app.py` | `start_turn()` 分段渲染：非 pasted 段 `escape()`，pasted 段 `_markdown_lines()` |
| Web 预处理 | `frontend/src/markdown.js` | `stripPastedTags()` — 将 `<pasted>` 块转为 Markdown blockquote |
| Web 渲染 | `frontend/src/markdown.js` | `renderUserMessage()` — 预处理 + `renderMarkdown` |
| Web 入口 | `frontend/src/main.js` | `appendMessageItem()` 对 `style: "text"` 调用 `renderUserMessage` |

## Data Model

无新增数据模型。`<pasted>` 标签格式为固定字符串：

```
<pasted>\n{任意内容}\n</pasted>
```

一段消息中可能有 0 到 N 个 `<pasted>` 块，与手打文本交替出现。

```
pasted_block 格式:
├── 开标签: "<pasted>\n" (固定)
├── 内容: 任意多行文本 (可能含 Markdown)
└── 闭标签: "\n</pasted>" (固定)
```

## API Contract

### `split_pasted_segments(text: str) -> list[tuple[bool, str]]`

- **Path/Signature**: `src/voidx/ui/output/dock/formatting.py`
- **Input**: 含 `<pasted>` 标签的用户消息文本
- **Output**: 段列表，每段为 `(is_pasted: bool, content: str)`
- **行为**:
  - 用正则 `<pasted>\n(.*?)\n</pasted>`（DOTALL，非贪婪）匹配
  - 匹配到的块：`(True, 内部内容)`
  - 匹配块之间的文本：`(False, 原样文本)`
  - 无 `<pasted>` 标签时返回 `[(False, text)]`，零开销

**拆分示例：**

```text
输入:
fix this bug
<pasted>
def foo():
    pass
</pasted>
please help

输出:
[(False, "fix this bug\n"),
 (True,  "def foo():\n    pass"),
 (False, "\nplease help")]
```

#### 分段结果 → OutputNode 映射

`start_turn` 当前结构为 `header = lines[0]`、`body_lines = lines[1:]`。分段后需将多段交替拼进单一的 `header + body_lines`，规则如下：

| 段类型 | header 贡献 | body_lines 贡献 |
|--------|------------|-----------------|
| 非 pasted 段（首段） | `escape(首行)` | `escape(其余行)` |
| 非 pasted 段（非首段） | — | `escape(所有行)` |
| pasted 段 | — | `_markdown_lines(content, width)` 的每行经 `_ansi_line()` 包裹 |

**关键约束 — ANSI 前缀：** `_markdown_lines()` 返回的行含 ANSI 转义码（颜色、样式），必须用 `_ansi_line()`（即加 `ANSI_LINE_PREFIX` 前缀）包裹后才能放进 `body_lines`，否则 dock 渲染器不会解析 ANSI 码。参考现有实现 `_bash_markdown_lines()`（`nodes.py:318-324`）：

```python
return [_ansi_line(line) for line in _markdown_lines(markdown, width)]
```

pasted 段须遵循相同模式。非 pasted 段走 `escape()` 纯文本，不加前缀。

**header 取值规则：** header 取第一个段的首行（无论该段是否 pasted）。若首段是 pasted 段，header 取其 `_markdown_lines()` 结果的第一行（经 `_ansi_line` 包裹）；若首段是非 pasted 段，header 取 `escape(首行)`。这保证 header 始终反映消息开头内容。

### `stripPastedTags(text: str) -> str` (JS)

- **Path/Signature**: `frontend/src/markdown.js`
- **Input**: 含 `<pasted>` 标签的用户消息文本
- **Output**: 标签被移除，粘贴内容转为 Markdown blockquote（每行加 `> ` 前缀）
- **行为**:
  - 匹配 `<pasted>\n...\n</pasted>`（非贪婪，支持多段）
  - 提取内部内容，每行加 `> ` 前缀，首尾各加空行分隔
  - 非 `<pasted>` 部分原样保留
  - 无 `<pasted>` 标签时返回原文，零开销

**转换示例：**

```text
输入:
fix this bug
<pasted>
def foo():
    pass
</pasted>
please help

输出:
fix this bug

> def foo():
>     pass

please help
```

### `renderUserMessage(text: str) -> HTMLElement`

- **Path/Signature**: `frontend/src/markdown.js`
- **Input**: 用户消息原始文本（可能含 `<pasted>` 标签）
- **Output**: 渲染后的 DOM 元素
- **行为**:
  - 调用 `stripPastedTags(text)` 将 `<pasted>` 块转为 Markdown blockquote
  - 调用 `renderMarkdown(result)` 渲染为 Markdown
  - blockquote 会自动获得现有 CSS 样式（左边框 + muted 色）

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `<pasted>` 标签未闭合（无 `</pasted>`） | 正则不匹配，原样显示该段文本（降级为旧行为） |
| `<pasted>` 内容为空 | 渲染为空 blockquote，不影响其余文本 |
| 粘贴内容含不合法 Markdown | 交给渲染器处理，渲染器已有 try/catch 降级为纯文本 |
| 粘贴内容含 `>` 开头行（已有引用） | `stripPastedTags` 加 `> ` 前缀后变为 `> > ...`，形成嵌套引用。Markdown 渲染器原生支持嵌套 blockquote，视觉上会多一层缩进，可接受 |
| 粘贴内容含连续单换行段落 | `marked` 配置了 `breaks: true`（`markdown.js:26`），单换行会渲染为 `<br>`。blockquote 内同理，段落间距可能与原文略有差异，可接受 |
| 用户手打 `<pasted>` 文本 | 概率极低，本期不处理（会被误识别为粘贴块） |

## Testing

### TUI 端 — `split_pasted_segments` (pytest)

文件：`tests/test_ui/output/test_dock_formatting.py`（`split_pasted_segments` 单元测试）+ `tests/test_ui/tui/test_tui_output_streaming.py`（`start_turn` 分段渲染集成测试）

| 用例 | 输入 | 期望输出 |
|------|------|---------|
| 无标签 | `"hello world"` | `[(False, "hello world")]` |
| 单块 | `"fix\n<pasted>\ncode\n</pasted>\npls"` | 3 段：非 pasted + pasted + 非 pasted |
| 多块交替 | `"a\n<pasted>\nb\n</pasted>\nc\n<pasted>\nd\n</pasted>\ne"` | 5 段交替 |
| 空内容 | `"<pasted>\n\n</pasted>"` | `[(True, "")]` + 前后非 pasted 段 |
| 未闭合标签 | `"fix\n<pasted>\ncode\npls"` | 正则不匹配，`[(False, 全文)]` |
| 块在开头 | `"<pasted>\ncode\n</pasted>\npls"` | 首段为 pasted |

### Web 端 — `stripPastedTags` / `renderUserMessage` (vitest)

文件：`frontend/test/markdown.test.js`

| 用例 | 验证点 |
|------|--------|
| 无标签 | 返回原文（零开销，`===` 原文） |
| 单块转 blockquote | 输出含 `> code`，标签已移除 |
| 多块 | 每块独立转 blockquote，非粘贴部分原样 |
| 空内容 | 输出空 blockquote（`> `） |
| 未闭合标签 | 原文返回 |
| `>` 开头行 | 嵌套为 `> > ...`，不报错 |
| `renderUserMessage` DOM | 返回 `.markdown-body` 元素，含 `<blockquote>` 子节点 |

### 集成验证

- TUI：粘贴多行代码后提交，确认回显中 pasted 块为 Markdown 渲染（有语法高亮），手打部分为纯文本，无 `<pasted>` 标签文本残留
- Web：同上，确认 blockquote 样式（左边框 + muted 色）生效

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 在渲染层转换，不修改生成层 | 在 `input.py` 生成层去掉标签 | `<pasted>` 标签对 LLM 有语义价值，应保留 |
| 用 Markdown blockquote 表示粘贴块 | 1. 直接去掉标签无视觉区分 2. 自定义 HTML 卡片组件 | blockquote 复用现有 CSS，两端（TUI/Web）渲染器都原生支持，实现最简 |
| Web 端 JS 实现独立版本 | 通过 gateway 传预处理后文本 | Web 端本地回显（`main.js:331`）在发送前就渲染，无法依赖后端预处理 |
| TUI 分段渲染，Web 统一渲染 | 两端都用统一预处理 | TUI 的 `start_turn` 用 `escape()` 纯文本，整条走 Markdown 会误渲染手打特殊字符；Web 端 `renderMarkdown` 本就处理整条消息，blockquote 方案更自然 |

## Resolved Questions

**Q: TUI 端 `start_turn` 改为 Markdown 渲染后，用户手打的特殊字符（`*` `_` `#`）是否会被误渲染？**

A: 是。因此 TUI 端采用**分段渲染**策略：
- 将消息文本按 `<pasted>` 块拆分为段
- 非 `<pasted>` 段：保持现有 `escape()` 纯文本行为（header/body_lines）
- `<pasted>` 段：内容用 `_markdown_lines()` 渲染为 Markdown，作为 turn node 的 body_lines

这样手打文本不受影响，只有粘贴内容走 Markdown 渲染。`start_turn`（`dock/app.py:215`）需重构为分段处理。

## Open Questions

- [x] 无

## Notes

### Web 端用户消息渲染路径

Web 端用户消息的渲染依赖**本地提交路径**（`main.js:333` 的 `appendMessageItem`），而非 `turn.started` 事件。数据流如下：

- `turn_runner.py:124` 发出 `TurnStarted(text=turn_display_text)`，`turn_display_text` 含 `<pasted>` 标签
- gateway adapter（`adapter.py:422`）将含标签的 `text` 原样转发为 `turn.started` 事件
- Web 端 `main.js:127` 对 `turn.started` 只调用 `setRunning(true)`，**忽略 `text` 字段**
- Web 端用户消息在本地提交时（`main.js:333`）调用 `appendMessageItem` → `renderUserMessage` 渲染，此时 `text` 为原始输入（含 `<pasted>` 标签），`renderUserMessage` 正确处理

当前行为正确。若未来 Web 端改为从 `turn.started` 事件渲染用户消息（例如多客户端同步场景），需在该路径也调用 `renderUserMessage`，否则 `<pasted>` 标签文本会泄漏到 UI。
