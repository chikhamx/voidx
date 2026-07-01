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

用户消息右浮、AI/Tool 左浮通过 flexbox + `align-self` 实现，不用 float：

```css
.transcript {
  display: flex;
  flex-direction: column;
  gap: var(--vx-space-2);
}

.message-user {
  align-self: flex-end;
  max-width: 80%;
  background: var(--vx-bg-elevated);
  border-radius: var(--vx-radius-md);
  padding: var(--vx-space-2) var(--vx-space-3);
}

.stream-buffer {
  align-self: flex-start;
  max-width: 85%;
  /* 无 border-left，无 background */
}

.tool-item {
  align-self: flex-start;
  width: 100%;
  /* 无 border，无 background */
}

.thought-item {
  align-self: flex-start;
  width: 100%;
  /* 无 border，无 background */
}
```

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
.diff-chevron { transition: transform 0.15s; }
.diff-item:not(.collapsed) .diff-chevron { transform: rotate(90deg); }
.diff-body {
  margin-top: var(--vx-space-1);
  font-family: var(--vx-font-mono);
  font-size: var(--vx-text-xs);
  overflow-x: auto;
}
.diff-item.collapsed .diff-body { display: none; }
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

**修复**：在 `append_message`（`src/voidx/ui/output/dock/nodes.py`）中，将 `style` 存入 `payload`：

```python
# nodes.py, append_message 方法中：
node = self._new_settled_node(
    target,
    before_active_stream=parent is None,
    node_type="message",
    header=header,
    body_lines=body_lines,
    collapsed=False,
    payload={"style": style} if style else {},  # 新增
)
```

同样，`append_thought` 已在 `payload` 中无 style（它是 `node_type="thought"`，不是 `message`），但 `DockEventConsumer` 处理 `ThoughtAppended` 时调用 `dock.append_thought(text, elapsed)`，`append_thought` 创建 `node_type="thought"` 节点。这条路径是正确的——`thought` 在 snapshot 中是独立的 `node_type`，不需要从 `payload.style` 推断。

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

> **注意**：`thought`、`error`、`warn`、`diff` 在 snapshot 中是独立的 `node_type`，但在 Item 路径中它们都映射为 `kind: "message"` + 不同的 `style`。这意味着 snapshot 路径和 Item 路径的分发逻辑不同：
> - **Snapshot 路径**：按 `node.node_type` 分发（`thought` → `appendThoughtItem`，`error` → `appendNoticeItem`）
> - **Item 路径**：在 `handleItem` 的 `kind === "message"` 分支内，按 `data.style` 二次分发（`style === "thought"` → `appendThoughtItem`，`style === "error"` → `appendNoticeItem`）

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

  // 保留尚未被 snapshot 包含的已提交 stream 元素（连接恢复时可能有
  // stream 在 commit 后但 snapshot 尚未刷新的情况）
  const committed = takeCommittedStreams();

  const byId = new Map(snapshot.nodes.map(n => [n.id, n]));
  for (const node of snapshot.nodes) {
    switch (node.node_type) {
      case "message": {
        const style = node.payload?.style || "text";
        const text = node.payload?.raw_text
          ?? [node.header, ...node.body_lines].join("\n");
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
      case "tool_result":
        // 追加到对应 tool_item 的 body（通过 tool_call_id 匹配）
        handleToolItem("item.delta", node.id, {
          tool_call_id: node.tool_call_id,
          detail: node.body_lines.join("\n"),
        });
        break;
      case "thought":
        appendThoughtItem(node.id, {
          text: node.body_lines.join("\n"),
          meta: node.meta,
        });
        break;
      case "error":
        appendNoticeItem(node.id, {
          style: "error",
          text: stripRichMarkup(node.header),
        });
        break;
      case "warn":
        appendNoticeItem(node.id, {
          style: "warning",
          text: stripRichMarkup(node.header),
        });
        break;
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

### `appendMessageItem(itemId, data)` — 改造

**变更**：
- `data.style === "text"` → 添加 `message-user` class（右浮），用 `renderUserMessage` 渲染
- `data.style === "markdown"` 或 `"guidance"` → 添加 `message-user` class（右浮），用 `renderMarkdown` 渲染
- `data.style === "ansi"` → 添加 `message-ansi` class（左浮），用 `renderMarkdown` 渲染（ansi 内容已在后端处理）
- 其他 style 不再走此函数（由 `handleItem` 二次分发到对应函数）

### `handleToolItem(method, itemId, data)` — 改造

**变更**：
- 折叠态改为单行：`chevron + tool_name + args_summary + status + elapsed`
- 去掉 `border` 和 `background`
- `tool-body` 默认 `hidden`，点击 header 切换
- `item.started` 时创建的 header 增加 `tool-chevron`（▸）和 `tool-args-summary`（从 `data.args` 提取摘要）
- `item.completed` 时将 `tool-spinner` 替换为 `tool-status`，追加 `tool-elapsed`

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
- `data.text` 按行分割，每行根据前缀（`+`/`-`/`@@`/`+++`/`---`）添加对应 class（复用现有 `renderDiffBlock` 逻辑）
- 追加到 `transcriptEl` 并滚动到底部

### `renderNodeElement(node, byId)` — 废弃

不再被 `renderTranscript` 调用。如果 `render.js` 中没有其他调用方，可以删除。

> **注意**：`renderNodeElement` 中的 `renderDiffBlock`、`diffLineClass`、`formatToolMeta`、`formatElapsed`、`stripRichMarkup` 等工具函数仍被其他代码使用，删除 `renderNodeElement` 时需保留这些函数。`stripRichMarkup` 和 `formatElapsed` 已被 export，`renderDiffBlock` 的逻辑需迁移到 `appendDiffItem` 中。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| Snapshot 中出现未知 node_type | 跳过，不渲染，不报错 |
| Tool item 的 `tool_call_id` 在 snapshot 中找不到对应 tool_call | `handleToolItem` 的 `item.delta` 分支增加 null 检查，打印 warning，不静默丢失 |
| Snapshot 恢复后实时 Item 到达，DOM id 冲突 | Item 路径已有 `data-item-id` / `data-tool-id` 去重逻辑，snapshot 恢复后清空 streams map（`_resetForTest` 模式或新增 `clearStreams` 导出） |
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

## Open Questions

- [x] Snapshot 恢复也走统一布局？→ **是，用户确认"要恢复原样"**
- [x] Tool 默认折叠？→ **是，点击展开，与 TUI 一致**
- [x] Thought 如何处理？→ **保留，默认折叠，可展开**
- [x] `subagent` 节点在桌面端如何渲染？→ **暂不渲染（跳过）**。当前 `handleItem` 中 subagent 直接 return，snapshot 路径也跳过。桌面端暂无 subagent UI 组件，后续可按需添加。
- [x] `diff` 节点在 snapshot 恢复时如何处理？→ **新增 `appendDiffItem` 函数渲染**。snapshot 中 `node_type === "diff"` 的节点走 `appendDiffItem`（左浮折叠）；`tool_call` 节点的 `payload.diff_text` 走 `handleToolItem` 的 `item.delta` 分支（作为 tool body 内容）。Item 路径中 `style === "diff"` 的 message 走 `handleItem` 二次分发到 `appendDiffItem`。
