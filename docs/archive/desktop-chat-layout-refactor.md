> **Status: Done**

# 桌面端主对话框布局重构 — 技术设计文档

## Context

桌面端主对话框（`#transcript`）当前有两套并行的渲染路径，视觉风格不一致：

1. **v2 Item 流式渲染**（`main.js` 的 `handleItem`）— 实时接收 `item.started/delta/completed`，直接创建 DOM 追加到 transcript。用户消息、AI 流式消息、Tool 消息各有不同的 DOM 结构和样式。
2. **v1 Snapshot 全量渲染**（`render.js` 的 `renderTranscript`）— 收到 `workspace.snapshot` 时用 `renderNodeElement` 重建整个 transcript，所有节点统一为 `article.node`，带左边框、缩进树、折叠箭头。

问题：
- 用户消息没有右对齐，和 AI 消息混在一起，难以区分对话轮次
- Tool 消息用卡片样式（border + background），视觉噪音大
- Snapshot 路径和 Item 路径产出的 DOM 结构不同，切换时视觉跳变
- `article.node` 的左边框 + 缩进树形布局在桌面端显得拥挤

目标：统一到一套渲染路径，用户消息右浮，AI/Tool 消息左浮且可折叠，去掉卡片边框，使界面简洁。

## Goals and Non-Goals

### Goals

- 用户消息右浮，轻微背景色区分，无边框
- AI 消息左浮，纯 markdown 渲染，无边框无左边框
- Tool 消息左浮，默认折叠（单行摘要），点击展开详情，无边框无卡片背景
- Thought 节点保留，默认折叠，斜体灰色，点击展开
- Snapshot 恢复和实时 Item 流产出一致的 DOM 结构和布局
- 去掉 `article.node` 的左边框和树形缩进

### Non-Goals

- 不修改总体布局（sidebar + main + dock 三栏不变）
- 不修改 TUI 端渲染逻辑
- 不修改后端协议或事件系统的传输格式（`item.started/delta/completed` 的 JSON-RPC 结构不变）
- 不修改 composer、dock、sidebar 等其他 UI 区域
- 不引入前端框架（React/Vue），仍在原生 JS 范围内

> **注意**：虽然不修改传输协议，但需要在后端 `OutputNode.payload` 中补充原始文本字段（详见 Data Model 节）。这是数据存储层的变更，不影响协议格式。

## Architecture

### 渲染路径统一

当前两条路径产出不同 DOM，需要统一。方案：**让 `renderTranscript`（snapshot 路径）复用 Item 路径的渲染函数**，而不是自己用 `renderNodeElement` 重建。

```
workspace.snapshot (连接/恢复)
    │
    ▼
renderTranscript(root, snapshot)
    │
    ├── 遍历 snapshot.nodes
    │     ├── node_type === "message"
    │     │     → appendMessageItem(itemId, data)     ← 用户消息右浮
    │     ├── node_type === "assistant"
    │     │     → appendStreamText + commitStream     ← AI 消息左浮
    │     ├── node_type === "tool_call" / "tool_result"
    │     │     → handleToolItem(模拟 started/completed)  ← Tool 折叠
    │     ├── node_type === "thought"
    │     │     → appendThoughtItem(itemId, data)     ← Thought 折叠
    │     ├── node_type === "error" / "warn"
    │     │     → appendNoticeItem(itemId, data)      ← 错误/警告左浮
    │     ├── node_type === "diff"
    │     │     → appendDiffItem(itemId, data)        ← Diff 左浮折叠
    │     └── 其他 → 跳过 (root/turn/startup/todo/status/permission/checkpoint/subagent)
    │
    ▼
#transcript (统一 DOM 结构)

item.started / item.delta / item.completed (实时)
    │
    ▼
handleItem(method, params)
    │
    ▼
#transcript (同一套 DOM 结构)
```

> **关键约束**：snapshot 路径复用 Item 渲染函数的前提是数据格式兼容。当前 `assistant` 节点的 `body_lines` 存储的是 ANSI 渲染文本（经 `_ansi_line` 处理），而 Item 路径的 `appendStreamText` 需要原始 markdown 文本。`message` 节点的 `payload` 不包含 `style` 字段。这两个问题需要在 Data Model 节解决后才能实现路径统一。

### DOM 结构

#### 用户消息（右浮）

```html
<div class="message-item message-user" data-item-id="...">
  <div class="markdown-body">...用户文本...</div>
</div>
```

#### AI 消息（左浮，无边框）

```html
<div class="stream-buffer" data-stream-id="...">
  <div class="stream-thinking">...思考文本（可选）...</div>
  <div class="markdown-body">...AI 回复...</div>
</div>
```

#### Tool 消息（左浮，折叠态）

```html
<div class="tool-item" data-tool-id="..." data-item-id="...">
  <div class="tool-header">
    <span class="tool-chevron">▸</span>
    <span class="tool-name">read</span>
    <span class="tool-args-summary">src/main.py</span>
    <span class="tool-status">done</span>
    <span class="tool-elapsed">0.2s</span>
  </div>
  <div class="tool-body" hidden>
    <pre class="tool-args">...完整参数...</pre>
    <pre class="tool-detail">...输出结果...</pre>
  </div>
</div>
```

#### Thought 消息（左浮，折叠态）

```html
<div class="thought-item" data-item-id="...">
  <div class="thought-header">
    <span class="thought-chevron">▸</span>
    <span class="thought-label">thought</span>
  </div>
  <div class="thought-body" hidden>
    <div class="markdown-body">...思考内容...</div>
  </div>
</div>
```

#### Error/Warn 消息（左浮）

```html
<div class="notice-item notice-error" data-item-id="...">
  <span class="notice-icon">✗</span>
  <span class="notice-text">...错误文本...</span>
</div>
```

`notice-warn` class 变体使用 `!` 图标和黄色配色。

#### Diff 消息（左浮，折叠态）

```html
<div class="diff-item" data-item-id="...">
  <div class="diff-header">
    <span class="diff-chevron">▸</span>
    <span class="diff-title">diff</span>
  </div>
  <div class="diff-body" hidden>
    <pre class="diff-content">...diff 文本...</pre>
  </div>
</div>
```

### CSS 布局策略

用户消息右浮、AI/Tool 左浮通过 flexbox + `align-self` 实现，不用 float。

**显隐策略统一**：所有折叠组件（tool/thought/diff）的 body 默认 `hidden`，点击 header 切换 `hidden` 属性（不使用 `collapsed` class）。CSS 只需 `[hidden] { display: none; }`（浏览器默认行为），无需额外折叠规则。

```css
.transcript {
  display: flex;
  flex-direction: column;
  gap: var(--vx-space-2);
}

/* === 用户消息（右浮） === */
.message-user {
  align-self: flex-end;
  max-width: 80%;
  background: var(--vx-bg-elevated);
  border-radius: var(--vx-radius-md);
  padding: var(--vx-space-2) var(--vx-space-3);
}

/* === ANSI 消息（左浮，无背景无边框） === */
.message-ansi {
  align-self: flex-start;
  max-width: 85%;
  font-family: var(--vx-font-mono);
  font-size: var(--vx-text-xs);
  white-space: pre-wrap;
}

/* === AI 消息（左浮，无边框） === */
.stream-buffer {
  align-self: flex-start;
  max-width: 85%;
  /* 无 border-left，无 background */
}

/* === Tool 消息（左浮，无边框无卡片背景） === */
.tool-item {
  align-self: flex-start;
  width: 100%;
  /* 无 border，无 background */
}
.tool-header {
  display: flex;
  align-items: center;
  gap: var(--vx-space-2);
  cursor: pointer;
  user-select: none;
  color: var(--vx-text-muted);
  font-size: var(--vx-text-sm);
}
.tool-header:hover { color: var(--vx-text-primary); }
.tool-chevron { transition: transform 0.15s; }
.tool-item:not([hidden]) .tool-chevron { transform: rotate(90deg); }
.tool-name { color: var(--vx-text-primary); font-weight: 600; }
.tool-args-summary { color: var(--vx-text-dim); }
.tool-status { margin-left: auto; font-size: var(--vx-text-xs); }
.tool-status.ok { color: var(--vx-success); }
.tool-status.err { color: var(--vx-error); }
.tool-status.running { color: var(--vx-warning); }
.tool-elapsed { color: var(--vx-text-muted); font-size: var(--vx-text-xs); }
.tool-body {
  margin-top: var(--vx-space-1);
  font-family: var(--vx-font-mono);
  font-size: var(--vx-text-xs);
  overflow-x: auto;
}
.tool-args, .tool-detail {
  background: var(--vx-bg-base);
  border-radius: var(--vx-radius-sm);
  color: var(--vx-text-dim);
  margin-top: var(--vx-space-1);
  padding: var(--vx-space-2);
  white-space: pre;
}

/* === Thought 消息（左浮，折叠态） === */
.thought-item {
  align-self: flex-start;
  width: 100%;
  /* 无 border，无 background */
}
.thought-header {
  display: flex;
  align-items: center;
  gap: var(--vx-space-2);
  cursor: pointer;
  user-select: none;
  color: var(--vx-text-muted);
  font-size: var(--vx-text-sm);
}
.thought-header:hover { color: var(--vx-text-primary); }
.thought-chevron { transition: transform 0.15s; }
.thought-item:not([hidden]) .thought-chevron { transform: rotate(90deg); }
.thought-label { font-style: italic; }
.thought-body {
  margin-top: var(--vx-space-1);
  color: var(--vx-text-dim);
  font-style: italic;
}

/* === Error/Warn 消息（左浮，单行） === */
.notice-item {
  align-self: flex-start;
  max-width: 85%;
  display: flex;
  align-items: center;
  gap: var(--vx-space-2);
  padding: var(--vx-space-1) var(--vx-space-2);
}
.notice-error { color: var(--vx-error); }
.notice-warn { color: var(--vx-warning); }
.notice-icon { font-weight: 700; }
.notice-text { font-size: var(--vx-text-sm); }

/* === Diff 消息（左浮，折叠态） === */
.diff-item {
  align-self: flex-start;
  width: 100%;
  /* 无 border，无 background */
}
.diff-header {
  display: flex;
  align-items: center;
  gap: var(--vx-space-2);
  cursor: pointer;
  user-select: none;
  color: var(--vx-text-muted);
  font-size: var(--vx-text-sm);
}
.diff-header:hover { color: var(--vx-text-primary); }
.diff-chevron { transition: transform 0.15s; }
.diff-item:not([hidden]) .diff-chevron { transform: rotate(90deg); }
.diff-body {
  margin-top: var(--vx-space-1);
  font-family: var(--vx-font-mono);
  font-size: var(--vx-text-xs);
  overflow-x: auto;
}
```

## Data Model

### 后端变更：`OutputNode.payload` 补充原始文本字段

当前 snapshot 路径和 Item 路径的数据格式不兼容，需要后端在 `OutputNode.payload` 中存储原始文本，使 snapshot 恢复时能复用 Item 渲染函数。

#### 问题 1：`assistant` 节点 `body_lines` 是 ANSI 渲染文本

`DockEventConsumer` 处理 `AssistantStreamUpdated` 事件时，调用 `dock.set_stream(e.text, ...)`，`_update_stream_node` 将原始 markdown 文本经 `_markdown_lines` + `_ansi_line` 处理后存入 `body_lines`。而 Item 路径的 `appendStreamText` 需要原始 markdown 文本。

**修复**：在 `_update_stream_node`（`src/voidx/ui/output/dock/stream.py`）中，将原始 markdown 文本存入 `payload["raw_text"]`：

```python
# stream.py, _update_stream_node 方法中，text phase 分支：
self._stream_node.payload["raw_text"] = clean  # 原始 markdown 文本
```

snapshot 恢复时，前端从 `node.payload.raw_text` 读取原始文本传给 `appendStreamText`。

#### 问题 2：`message` 节点 `payload` 不包含 `style`

`DockEventConsumer` 处理 `MessageAppended` 事件时，调用 `dock.append_message(text, style=style)`，但 `append_message`（`nodes.py:50-74`）不将 `style` 写入 `payload`，只用于 rich markup 标签。snapshot 恢复时前端无法区分 `text`/`markdown`/`guidance`/`thought`/`warning`/`error`/`diff` 等 style。

**修复**：在 `append_message`（`src/voidx/ui/output/dock/nodes.py`）中，将 `style` 存入 `payload`（同时支持可选的 `title` 参数，详见问题 5）：

```python
# nodes.py, append_message 方法中：
def append_message(self, text: str, *, style: str = "", parent: OutputNode | None = None, markup: bool = False, title: str = "") -> OutputNode | None:
    # ... 现有文本处理逻辑 ...
    payload = {"style": style} if style else {}
    if title:
        payload["title"] = title
    node = self._new_settled_node(
        target,
        before_active_stream=parent is None,
        node_type="message",
        header=header,
        body_lines=body_lines,
        collapsed=False,
        payload=payload,  # 新增
    )
```

`append_thought` 创建 `node_type="thought"` 节点（非 `message`），`DockEventConsumer` 处理 `ThoughtAppended` 时调用 `dock.append_thought(text, elapsed)`。这条路径是正确的——`thought` 在 snapshot 中是独立的 `node_type`，不需要从 `payload.style` 推断。

但 Item 路径中，`ThoughtAppended` 事件经 adapter 映射为 `kind: "message", style: "thought"`（`adapter.py:224-228`），前端 `handleItem` 走 `kind === "message"` 分支调用 `appendMessageItem`。**需要改造 `handleItem`**，在 `kind === "message"` 分支中对 `data.style === "thought"` 做特殊分发（详见 API Contract）。

#### 问题 3：`MarkdownAppended` 事件在 dock 中走 `capture` 路径

`DockEventConsumer` 处理 `MarkdownAppended` 时调用 `dock.capture(lambda console: console.print(Markdown(content)))`，`capture` → `append_ansi` → 创建 `node_type="message"` 节点，`body_lines` 是 ANSI 渲染后的 Markdown。而 adapter 发送 `kind: "message", style: "markdown"`，`data.text` 是原始 markdown 内容。

**修复**：在 `DockEventConsumer` 处理 `MarkdownAppended` 的 case（`consumers.py:159-160`）中，改为调用 `dock.append_message(content, style="markdown")`：

```python
# consumers.py:
case MarkdownAppended(content=content):
    return self._dock.append_message(content, style="markdown")
```

这样 `message` 节点的 `body_lines` 是 escape 后的纯文本（不是 ANSI），`payload.style` 为 `"markdown"`，前端可直接用 `renderMarkdown` 渲染。

> **注意**：`append_message` 的 `markup=False` 默认路径会对文本做 `escape()` 处理，产出的 `body_lines` 是 HTML 转义后的纯文本。前端 `renderMarkdown` 可以处理 HTML 转义文本，但为保险起见，可在 `payload` 中额外存储 `raw_text`（原始未转义文本），snapshot 恢复时优先使用 `payload.raw_text`。

#### 问题 4：`WarningAppended` 的 `style` 值与 adapter 不匹配

`DockEventConsumer` 处理 `WarningAppended` 时调用 `dock.append_message(f"! {message}", style="yellow")`（`consumers.py:163-164`）。问题 2 的修复会将 `style` 存入 `payload`，但存入的值是 `"yellow"`（rich markup 颜色名），而 adapter 的 `_on_warning` 发送的是 `style: "warning"`（`adapter.py:230-233`）。两条路径的 `style` 值不一致，snapshot 恢复时前端检查 `style === "warning"` 不会匹配 `"yellow"`，warning 消息会被错误地右浮渲染为用户消息。

此外，`append_message` 会在文本前拼接 `"! "` 前缀（由 consumers 传入），而前端 `appendNoticeItem` 也会添加 `!` 图标，导致重复。

**修复**：在 `DockEventConsumer` 处理 `WarningAppended` 的 case 中，改为传入语义 style 并去掉前缀：

```python
# consumers.py:
case WarningAppended(message=message):
    return self._dock.append_message(message, style="warning")
```

这样 `payload.style` 为 `"warning"`，与 adapter 一致；`appendNoticeItem` 负责添加 `!` 图标，不会重复。

#### 问题 5：`DiffAppended` 事件在 dock 中走 `capture` 路径，不创建 `node_type="diff"` 节点

`DockEventConsumer` 处理 `DiffAppended` 时调用 `dock.capture(lambda console: render_diff(console, e.diff_text, e.title))`（`consumers.py:169-172`），`capture` → `append_ansi` → 创建 `node_type="message"` 节点，`body_lines` 是 ANSI 渲染文本。而 adapter 发送 `kind: "message", style: "diff"`，`data.text` 是原始 diff 文本。

`node_type="diff"` 节点仅在 `via_events()` 为 `False` 时由 `capture.py:139-143` 创建。桌面端网关走事件路径（`via_events()` 为 `True`），所以 snapshot 中**不会**出现 `node_type="diff"` 节点。文档节点类型映射表中的 `diff (snapshot node_type)` 行在事件路径下不会触发。

**修复**：在 `DockEventConsumer` 处理 `DiffAppended` 的 case 中，改为调用 `dock.append_message`：

```python
# consumers.py:
case DiffAppended() as e:
    return self._dock.append_message(e.diff_text, style="diff")
```

这样 `payload.style` 为 `"diff"`，`body_lines` 是 escape 后的纯文本，前端可通过 `appendDiffItem` 渲染。`append_message` 不支持 `title` 参数，`title` 可存入 `payload`：

```python
# nodes.py, append_message 方法中，新增可选 title 参数：
def append_message(self, text: str, *, style: str = "", parent: OutputNode | None = None, markup: bool = False, title: str = "") -> OutputNode | None:
    # ... 现有逻辑 ...
    payload = {"style": style} if style else {}
    if title:
        payload["title"] = title
    node = self._new_settled_node(
        target,
        before_active_stream=parent is None,
        node_type="message",
        header=header,
        body_lines=body_lines,
        collapsed=False,
        payload=payload,
    )
```

> **注意**：`node_type="diff"` 和 `node_type="warn"` 节点在非事件路径（`via_events()` 为 `False`，如 TUI 直接渲染）下仍会被创建。`renderTranscript` 的 `case "diff":` 和 `case "warn":` 分支保留用于兼容这些场景，但桌面端事件路径下不会触发。

#### 问题 6：`append_error` 的 header 包含 `✗` 图标前缀，snapshot 恢复时与 `appendNoticeItem` 重复

`append_error`（`nodes.py:76-91`）创建 `node_type="error"` 节点，`header=f"[red]✗ {escape(lines[0])}[/red]"`。snapshot 恢复时 `stripRichMarkup(node.header)` 保留 `✗ ` 前缀，而 `appendNoticeItem` 也会添加 `✗` 图标，导致重复。

**修复**：在 `append_error` 中将原始消息存入 `payload.raw_text`：

```python
# nodes.py, append_error 方法中：
node = self._new_settled_node(
    parent or self._tree.root,
    before_active_stream=parent is None,
    node_type="error",
    header=f"[red]✗ {escape(lines[0])}[/red]",
    body_lines=[f"[red]  {escape(line)}[/red]" for line in lines[1:]],
    collapsed=False,
    status="error",
    payload={"raw_text": clean},  # 新增：原始未格式化文本
)
```

前端 `renderTranscript` 的 `case "error":` 分支优先使用 `payload.raw_text`，回退到 `stripRichMarkup(node.header).replace(/^[✗!]\s*/, "")` 去掉前缀（兼容旧数据）。

#### 问题 7：`append_thought` 不存储 `payload.raw_text`

`append_thought`（`nodes.py:109-137`）创建 `node_type="thought"` 节点，`body_lines` 是 `[f"[dim]{escape(line)}[/dim]" for line in visible_lines]`。snapshot 恢复时 `stripRichMarkup` 会去掉 `[dim]` 标签，但 `escape(line)` 产生的 HTML 转义文本经 `renderMarkdown` 渲染可能不正确（如 `&lt;` 会被当作实体而非原始字符）。

**修复**：在 `append_thought` 中将原始文本存入 `payload.raw_text`：

```python
# nodes.py, append_thought 方法中：
node = self._tree.new_node(
    parent=parent or self.ensure_agent(),
    node_type="thought",
    header=f"[dim]●[/dim] [dim]{escape(summary)}[/dim]",
    body_lines=body,
    collapsed=False,
    meta=summary,
    payload={"raw_text": clean},  # 新增：原始未格式化文本
)
```

前端 `renderTranscript` 的 `case "thought":` 分支优先使用 `payload.raw_text`，回退到 `stripRichMarkup(body_lines.join("\n"))`（兼容旧数据）。

#### 问题 8：`append_tool_result` 不存储 `payload.raw_text`

`append_tool_result`（`nodes.py:202-233`）创建 `node_type="tool_result"` 节点，`body_lines` 是 `[escape(line) for line in lines[1:]]`。与 `append_thought` 同理，escape 后的文本经 `renderMarkdown` 渲染可能不正确。

**修复**：在 `append_tool_result` 中将原始文本存入 `payload.raw_text`：

```python
# nodes.py, append_tool_result 方法中：
node = self._tree.new_node(
    parent=target,
    node_type="tool_result",
    header=escape(lines[0]) if lines else "",
    body_lines=[escape(line) for line in lines[1:]],
    collapsed=collapsed,
    tool_call_id=tool_call_id,
    payload={"raw_text": clean},  # 新增：原始未格式化文本
)
```

前端 `renderTranscript` 的 `case "tool_result":` 分支优先使用 `payload.raw_text`，回退到 `stripRichMarkup(body_lines.join("\n"))`（兼容旧数据）。

### 节点类型 → 渲染映射

| node_type / item kind | 渲染函数 | 布局 | 默认折叠 |
|----------------------|----------|------|----------|
| `message` (style=text) | `appendMessageItem` | 右浮 | 否 |
| `message` (style=markdown) | `appendMessageItem` | 右浮 | 否 |
| `message` (style=guidance) | `appendMessageItem` | 右浮 | 否 |
| `message` (style=thought) | `appendThoughtItem` | 左浮 | **是** |
| `message` (style=warning) | `appendNoticeItem`（notice-warn） | 左浮 | 否 |
| `message` (style=error) | `appendNoticeItem`（notice-error） | 左浮 | 否 |
| `message` (style=diff) | `appendDiffItem` | 左浮 | **是** |
| `message` (style=ansi) | `appendMessageItem`（style=ansi） | 左浮 | 否 |
| `assistant` / `assistant_stream` | `appendStreamText` + `commitStream` | 左浮 | 否 |
| `tool_call` / `tool` | `handleToolItem` | 左浮 | **是** |
| `tool_result` | 追加到对应 tool_item 的 body | — | — |
| `thought` (snapshot node_type) | `appendThoughtItem` | 左浮 | **是** |
| `error` (snapshot node_type) | `appendNoticeItem`（notice-error） | 左浮 | 否 |
| `warn` (snapshot node_type) | `appendNoticeItem`（notice-warn） | 左浮 | 否 |
| `diff` (snapshot node_type) | `appendDiffItem` | 左浮 | **是** |
| `root` / `turn` / `startup` / `todo` / `status` | 跳过 | — | — |
| `permission` / `checkpoint` / `subagent` | 跳过（桌面端暂不渲染） | — | — |

> **注意**：`thought`、`error` 在 snapshot 中是独立的 `node_type`，但在 Item 路径中它们映射为 `kind: "message"` + 不同的 `style`。这意味着 snapshot 路径和 Item 路径的分发逻辑不同：
> - **Snapshot 路径**：按 `node.node_type` 分发（`thought` → `appendThoughtItem`，`error` → `appendNoticeItem`）
> - **Item 路径**：在 `handleItem` 的 `kind === "message"` 分支内，按 `data.style` 二次分发（`style === "thought"` → `appendThoughtItem`，`style === "error"` → `appendNoticeItem`）
>
> **事件路径下的 `warn`/`diff` 节点类型**：桌面端网关走事件路径（`via_events()` 为 `True`），`WarningAppended` 和 `DiffAppended` 经问题 4/5 修复后走 `append_message(style="warning"/"diff")`，创建的是 `node_type="message"` 节点（非 `node_type="warn"/"diff"`）。`renderTranscript` 的 `case "warn":` 和 `case "diff":` 分支仅在非事件路径（TUI 直接渲染）下触发。事件路径下这些内容通过 `case "message":` 的 `style` 二次分发处理。

## API Contract

### `renderTranscript(root, snapshot)` — 改造

**当前签名**（不变）：
```js
export function renderTranscript(root, snapshot)
```

**当前行为**：遍历 `snapshot.nodes`，每个节点调用 `renderNodeElement` 创建 `article.node` DOM。

**变更后行为**：遍历 `snapshot.nodes`，按 `node_type` 分发到 Item 路径的渲染函数（`appendMessageItem`、`handleToolItem` 等），产出和实时 Item 流一致的 DOM。

**实现要点**：
```js
export function renderTranscript(root, snapshot) {
  root.replaceChildren();
  // 先取出已提交但未被 snapshot 包含的 stream 元素（连接恢复时可能有
  // stream 在 commit 后但 snapshot 尚未刷新的情况）
  const committed = takeCommittedStreams();
  // 清空活跃的未提交 stream，防止 DOM id 冲突
  clearActiveStreams();

  for (const node of snapshot.nodes) {
    switch (node.node_type) {
      case "message": {
        const style = node.payload?.style || "text";
        const text = node.payload?.raw_text
          ?? stripRichMarkup([node.header, ...node.body_lines].join("\n"));
        // 按 style 二次分发（与 handleItem 的 message 分支一致）
        if (style === "thought") {
          appendThoughtItem(node.id, { text, meta: node.meta });
        } else if (style === "error" || style === "warning") {
          appendNoticeItem(node.id, { style, text });
        } else if (style === "diff") {
          appendDiffItem(node.id, { text, title: node.payload?.title });
        } else {
          appendMessageItem(node.id, { style, text });
        }
        break;
      }
      case "assistant": {
        // 从 payload.raw_text 读取原始 markdown（后端已补充此字段）
        const rawText = node.payload?.raw_text
          ?? stripRichMarkup(node.body_lines.join("\n"));
        appendStreamText(node.id, rawText, "text");
        commitStream(node.id);
        break;
      }
      case "tool_call":
        handleToolItem("item.started", node.id, {
          tool_call_id: node.tool_call_id,
          tool_name: node.payload?.tool_name,
          args: node.payload?.args,
        });
        // 如果 tool_call 节点本身包含 diff_text（payload.diff_text），追加到 body
        if (node.payload?.diff_text) {
          handleToolItem("item.delta", node.id, {
            tool_call_id: node.tool_call_id,
            diff_text: node.payload.diff_text,
          });
        }
        handleToolItem("item.completed", node.id, {
          tool_call_id: node.tool_call_id,
          ok: node.status !== "error",
          elapsed: node.elapsed,
        });
        break;
      case "tool_result": {
        // append_tool_result 的 body_lines 是 escape() 后的文本，需 stripRichMarkup 清理。
        // 优先使用 payload.raw_text（如果有），回退到 stripRichMarkup(body_lines)。
        const detailText = node.payload?.raw_text
          ?? stripRichMarkup(node.body_lines.join("\n"));
        // 注意：此处依赖 tool_call 节点在 tool_result 之前遍历（snapshot 按树序排列）。
        // handleToolItem 的 item.delta 分支已包含 null 检查（详见 handleToolItem 改造节），
        // 若 tool_call_id 找不到对应 DOM 元素，会打印 warning 而非静默丢失。
        handleToolItem("item.delta", node.id, {
          tool_call_id: node.tool_call_id,
          detail: detailText,
        });
        break;
      }
      case "thought": {
        // append_thought 的 body_lines 包含 [dim] rich markup 标签，需 stripRichMarkup 清理。
        // 优先使用 payload.raw_text（如果有），回退到 stripRichMarkup(body_lines)。
        const thoughtText = node.payload?.raw_text
          ?? stripRichMarkup(node.body_lines.join("\n"));
        appendThoughtItem(node.id, {
          text: thoughtText,
          meta: node.meta,
        });
        break;
      }
      case "error": {
        // append_error 的 header 是 "[red]✗ {message}[/red]"，stripRichMarkup 后保留 "✗ " 前缀。
        // appendNoticeItem 会自己添加 ✗ 图标，需去掉 header 中的前缀避免重复。
        // 优先使用 payload.raw_text（后端补充），回退到 stripRichMarkup 后去掉前缀。
        const rawText = node.payload?.raw_text
          ?? stripRichMarkup(node.header).replace(/^[✗!]\s*/, "");
        appendNoticeItem(node.id, { style: "error", text: rawText });
        break;
      }
      case "warn": {
        // 同理，append_message(style="warning") 的 header 无前缀（问题 4 修复后），
        // 但旧数据可能包含 "! " 前缀，统一清理。
        const rawText = node.payload?.raw_text
          ?? stripRichMarkup(node.header).replace(/^[✗!]\s*/, "");
        appendNoticeItem(node.id, { style: "warning", text: rawText });
        break;
      }
      case "diff":
        appendDiffItem(node.id, {
          text: node.body_lines.join("\n"),
          title: node.header,
        });
        break;
      // root/turn/startup/todo/status/permission/checkpoint/subagent → 跳过
    }
  }

  // 追加未被 snapshot 包含的已提交 stream
  for (const el of committed) {
    if (el.isConnected) continue;
    root.append(el);
  }
}
```

### `handleItem(method, params)` — 改造

**当前行为**：`kind === "message"` 时统一调用 `appendMessageItem`，不区分 `data.style`。

**变更后行为**：在 `kind === "message"` 分支内，按 `data.style` 二次分发：

```js
if (kind === "message") {
  if (method === "item.started") {
    const style = data.style || "text";
    if (style === "thought") {
      appendThoughtItem(itemId, { text: data.text, meta: data.elapsed ? `Thinking for ${data.elapsed}s` : "Thinking" });
    } else if (style === "error" || style === "warning") {
      appendNoticeItem(itemId, { style, text: data.text });
    } else if (style === "diff") {
      appendDiffItem(itemId, { text: data.text, title: data.title });
    } else {
      appendMessageItem(itemId, data);
    }
  }
  return;
}
```

> **原因**：adapter 把 `ThoughtAppended`、`ErrorAppended`、`WarningAppended`、`DiffAppended` 事件全部映射为 `kind: "message"` + 不同 `style`（`adapter.py:224-250`）。当前前端不区分 style，全部走 `appendMessageItem` 右浮渲染，导致 thought/error/warn/diff 视觉上与用户消息混在一起。
>
> **`message` kind 的 method 范围**：adapter 对所有 `message` kind 的事件只发送 `item.started`（`adapter.py:224-250` 的 `_on_thought`/`_on_warning`/`_on_error`/`_on_diff_appended`/`_on_guidance` 等方法均调用 `_item_notification(..., "message", "started", ...)`）。`message` kind 不会有 `item.delta` 或 `item.completed`，因此 `handleItem` 的 `kind === "message"` 分支只处理 `item.started`，对其他 method 直接 `return`。

### `appendMessageItem(itemId, data)` — 改造

**变更**：
- `data.style === "text"` → 添加 `message-user` class（右浮），用 `renderUserMessage` 渲染
- `data.style === "markdown"` 或 `"guidance"` → 添加 `message-user` class（右浮），用 `renderMarkdown` 渲染
- `data.style === "ansi"` → 添加 `message-ansi` class（左浮），用 `renderMarkdown` 渲染。`ansi` style 不被 `handleItem` 二次分发拦截（只拦截 `thought`/`error`/`warning`/`diff`），因此仍走此函数。`message-ansi` 的 CSS 需设置 `align-self: flex-start`（左浮），与 `message-user` 的 `align-self: flex-end` 区分。ansi 内容已在后端处理为纯文本，前端直接渲染即可。
- `thought`/`error`/`warning`/`diff` style 不再走此函数（由 `handleItem` 二次分发到 `appendThoughtItem`/`appendNoticeItem`/`appendDiffItem`）

### `handleToolItem(method, itemId, data)` — 改造

**变更**：
- 折叠态改为单行：`chevron + tool_name + args_summary + status + elapsed`
- 去掉 `border` 和 `background`
- `tool-body` 默认 `hidden`，点击 header 切换 `hidden` 属性（与 thought/diff 一致，不使用 `collapsed` class）
- `item.started` 时创建的 header 增加 `tool-chevron`（▸）和 `tool-args-summary`（从 `data.args` 提取摘要）
- `item.completed` 时将 `tool-spinner` 替换为 `tool-status`，追加 `tool-elapsed`

> **注意**：`formatElapsed` 当前在 `main.js:457` 和 `render.js:158` 各有一份实现，签名不一致：`main.js` 版本接受毫秒（`ms < 1000`），`render.js` 版本接受秒（`seconds < 1`）。后端 `finish_tool_node`（`nodes.py:196`）存储的 `elapsed` 是**秒**（`elapsed:.1f`）。本次改造应**统一为 `render.js` 版本**（接受秒），删除 `main.js` 中的重复实现，从 `render.js` import。`handleToolItem` 的 `item.completed` 分支使用 `formatElapsed(data.elapsed)` 时传入的是秒值。

**`tool_result` 匹配风险修复**：

当前 `handleToolItem` 通过 `document.querySelector([data-tool-id="..."])` 查找已有 tool 元素。在 snapshot 恢复场景下，如果 `tool_result` 节点在 `tool_call` 之前出现（遍历顺序异常），`querySelector` 返回 `null`，delta 分支被静默跳过。

**修复**：在 `item.delta` 分支开头增加 null 检查：

```js
} else if (el) {
  // ... 现有逻辑
} else if (method === "item.delta") {
  // tool_call 未找到，作为独立 tool item 渲染
  console.warn(`voidx: tool delta for unknown tool_call_id: ${data.tool_call_id}`);
}
```

### `appendThoughtItem(itemId, data)` — 新增

**职责**：创建折叠态的 thought 元素，点击 header 展开 body。

**签名**：
```js
export function appendThoughtItem(itemId, data)
// data: { text: string, meta?: string }
```

**实现要点**：
- 创建 `.thought-item` DOM（结构见 DOM 结构节）
- `thought-body` 默认 `hidden`
- header 点击事件切换 `thought-body` 的 `hidden` 属性
- `data.meta` 显示在 `thought-label` 中（如 "Thinking for 3s"）
- `data.text` 经 `renderMarkdown` 渲染后填入 `thought-body .markdown-body`
- 追加到 `transcriptEl` 并滚动到底部

### `appendNoticeItem(itemId, data)` — 新增

**职责**：创建 error/warn 通知元素（非折叠，单行）。

**签名**：
```js
export function appendNoticeItem(itemId, data)
// data: { style: "error" | "warning", text: string }
```

**实现要点**：
- `style === "error"` → class `notice-item notice-error`，图标 `✗`，颜色 `var(--vx-error)`
- `style === "warning"` → class `notice-item notice-warn`，图标 `!`，颜色 `var(--vx-warning)`
- `text` 经 `stripRichMarkup` 清理后填入 `notice-text`
- 追加到 `transcriptEl` 并滚动到底部

### `appendDiffItem(itemId, data)` — 新增

**职责**：创建折叠态的 diff 元素，点击 header 展开 body。

**签名**：
```js
export function appendDiffItem(itemId, data)
// data: { text: string, title?: string }
```

**实现要点**：
- 创建 `.diff-item` DOM（结构见 DOM 结构节）
- `diff-body` 默认 `hidden`
- header 点击事件切换 `diff-body` 的 `hidden` 属性
- `data.title` 显示在 `diff-title` 中，默认 `"diff"`
- `data.text` 按行分割，每行根据前缀（`+`/`-`/`@@`/`+++`/`---`）添加对应 class
- 追加到 `transcriptEl` 并滚动到底部

> **注意**：`renderDiffBlock` 当前在 `render.js:99` 和 `main.js:437` 各有一份实现，DOM 结构和 CSS class 不同（`render.js` 版本产出 `node-diff` class 的 `<pre>`，`main.js` 版本产出 `tool-diff` class 的 `<div>`）。本次改造应**统一为一份实现**，从 `render.js` export，`main.js` 和 `appendDiffItem` 都从 `render.js` import。统一版本使用 `diffLineClass`（`render.js:111`）判断每行 class，产出 `<pre class="diff-content">` 容器（与 DOM 结构节中的 `.diff-content` 一致）。删除 `main.js` 中的重复 `renderDiffBlock`。

### `renderNodeElement(node, byId)` — 废弃

不再被 `renderTranscript` 调用。如果 `render.js` 中没有其他调用方，可以删除。

> **注意**：`renderNodeElement` 中的 `renderDiffBlock`、`diffLineClass`、`formatToolMeta`、`formatElapsed`、`stripRichMarkup` 等工具函数仍被其他代码使用，删除 `renderNodeElement` 时需保留这些函数。`stripRichMarkup` 和 `formatElapsed` 已被 export，`renderDiffBlock` 需改为 export 并统一（详见 `appendDiffItem` 注意事项）。

### `clearActiveStreams()` — 新增

**职责**：清空 `stream.js` 内部的 `streams` Map（活跃的未提交 stream），用于 snapshot 恢复后防止 DOM id 冲突。不触碰 `committedEls`（已提交元素由 `takeCommittedStreams` 管理）。

**签名**：
```js
export function clearActiveStreams()
```

**实现要点**：
- 遍历 `streams` Map，移除每个 stream 的 DOM 元素（`el.remove()`）
- 清空 `streams` Map
- 在 `renderTranscript` 中 `takeCommittedStreams()` 之后调用（先取出已提交元素，再清空活跃 stream）

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| Snapshot 中出现未知 node_type | 跳过，不渲染，不报错 |
| Tool item 的 `tool_call_id` 在 snapshot 中找不到对应 tool_call | `handleToolItem` 的 `item.delta` 分支增加 null 检查，打印 warning，不静默丢失 |
| Snapshot 恢复后实时 Item 到达，DOM id 冲突 | `renderTranscript` 调用 `takeCommittedStreams()` 取出已提交元素后，调用 `clearActiveStreams()` 清空活跃 stream map，防止残留 stream 与新 DOM 冲突（详见 API Contract `clearActiveStreams`） |
| Thought 节点 body_lines 为空 | 折叠态显示 meta，展开态为空 |
| `assistant` 节点 `payload.raw_text` 缺失（旧数据兼容） | 回退到 `stripRichMarkup(body_lines.join("\n"))`，虽然不是纯 markdown 但可读 |
| `message` 节点 `payload.style` 缺失（旧数据兼容） | 回退到 `"text"`，按用户消息右浮渲染 |
| `message` 节点 `payload.raw_text` 缺失（旧数据兼容） | 回退到 `[node.header, ...node.body_lines].join("\n")`，经 `stripRichMarkup` 清理 |
| Snapshot 恢复时有已提交但未被 snapshot 包含的 stream | `renderTranscript` 调用 `takeCommittedStreams()` 保留这些元素，追加到 transcript 末尾 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 flexbox `align-self` 实现左右浮 | CSS `float` | flexbox 更可靠，不需要 clearfix，且 `transcript` 已是垂直布局 |
| Snapshot 路径复用 Item 渲染函数 | 统一用 snapshot 路径 | Item 流式路径是实时主路径，snapshot 仅在连接恢复时触发；让 snapshot 适配 Item 比反过来更合理 |
| Tool 默认折叠 | 默认展开 | 与 TUI 一致（TUI 中 tool_call `collapsed=True`），减少视觉噪音 |
| Thought 默认折叠 | 隐藏 / 默认展开 | 保留信息但减少干扰，用户可按需展开 |
| 去掉 `renderNodeElement` | 保留但简化 | 如果 snapshot 路径不再调用它，且没有其他调用方，保留只会增加维护负担 |
| 不引入前端框架 | 迁移到 React | 本次改动范围有限，引入框架是独立决策，不应耦合 |
| 后端 `payload` 补充 `raw_text` / `style` | 前端 ANSI→markdown 反向解析 | ANSI 渲染文本不可靠地反向解析为 markdown；在后端存储原始文本是单向、无损的 |
| `MarkdownAppended` 改走 `append_message` | 保持 `capture` 路径 | `capture` 产出 ANSI 文本，与 Item 路径的原始 markdown 不兼容；`append_message` 产出 escape 纯文本，可被 `renderMarkdown` 处理 |
| `handleItem` 中按 `data.style` 二次分发 | 新增 `kind` 类型 | adapter 协议已将 thought/error/warn/diff 映射为 `kind: "message"`，改协议影响面大；前端二次分发是局部变更 |
| `error`/`warn` 节点非折叠单行 | 折叠 | 错误/警告需要立即可见，不应隐藏在折叠中 |
| `diff` 节点默认折叠 | 默认展开 | diff 内容通常较长，折叠减少视觉噪音，与 tool_call 一致 |
| `permission`/`checkpoint`/`subagent` 暂不渲染 | 渲染 | 桌面端这些节点暂无对应 UI 组件，渲染为空壳不如跳过；后续可按需添加 |
| `WarningAppended` 改走 `append_message(style="warning")` | 保持 `style="yellow"` + `! ` 前缀 | `style="yellow"` 是 rich markup 颜色名，与 adapter 的 `style: "warning"` 不匹配；前缀由前端 `appendNoticeItem` 添加，避免重复 |
| `DiffAppended` 改走 `append_message(style="diff")` | 保持 `capture` 路径 | `capture` 产出 ANSI 文本且创建 `node_type="message"` 节点（非 `node_type="diff"`），与 Item 路径不兼容；`append_message` 产出 escape 纯文本，`payload.style="diff"` 可被前端正确分发 |
| `append_error` 补充 `payload.raw_text` | 前端 `stripRichMarkup` 后正则去前缀 | 后端存储原始文本是单向无损的；正则去前缀是兼容旧数据的回退方案，新数据应走 `raw_text` |
| `formatElapsed` 统一为 `render.js` 版本（接受秒） | 保留 `main.js` 版本（接受毫秒） | 后端 `elapsed` 存储的是秒；两份实现造成混淆，统一为一份减少维护负担 |
| `renderDiffBlock` 统一为 `render.js` 版本并 export | 保留两份独立实现 | 两份实现 DOM 结构和 CSS class 不同，容易不一致；统一后 `appendDiffItem` 和 `handleToolItem` 共用同一逻辑 |
| `clearActiveStreams` 只清活跃 stream 不清 committedEls | 清空全部 | `takeCommittedStreams` 需要取出已提交元素保留到 transcript 末尾，若先清空会丢失这些元素 |

## Open Questions

- [x] Snapshot 恢复也走统一布局？→ **是，用户确认"要恢复原样"**
- [x] Tool 默认折叠？→ **是，点击展开，与 TUI 一致**
- [x] Thought 如何处理？→ **保留，默认折叠，可展开**
- [x] `subagent` 节点在桌面端如何渲染？→ **暂不渲染（跳过）**。当前 `handleItem` 中 subagent 直接 return，snapshot 路径也跳过。桌面端暂无 subagent UI 组件，后续可按需添加。
- [x] `diff` 节点在 snapshot 恢复时如何处理？→ **新增 `appendDiffItem` 函数渲染**。snapshot 中 `node_type === "diff"` 的节点走 `appendDiffItem`（左浮折叠）；`tool_call` 节点的 `payload.diff_text` 走 `handleToolItem` 的 `item.delta` 分支（作为 tool body 内容）。Item 路径中 `style === "diff"` 的 message 走 `handleItem` 二次分发到 `appendDiffItem`。
