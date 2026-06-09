# Capture 非 Event 模式补全设计

> **Status: Draft**

## 问题

`OutputCapture` 的 `print()`、`markdown()`、`thinking()`、`sep()` 四个方法在非 event 模式下是空实现（`pass`），而同类的 `error()` 和 `warn()` 在两种模式下都有完整实现。这导致：

1. 非 event 模式（如 headless、测试、旧 TUI 路径）下，普通输出、Markdown、thinking 内容和分隔线完全丢失
2. 接口不对称：`error()`/`warn()` 两种模式都工作，其他方法只在 event 模式下工作
3. 新开发者难以判断这些 `pass` 是"有意忽略"还是"待实现"

## 目标

1. 补全 `print()`、`markdown()`、`thinking()`、`sep()` 在非 event 模式下的实现
2. 保持与 `error()`/`warn()` 一致的实现模式
3. 不改变 event 模式下的行为
4. 不引入新的依赖或渲染逻辑

## 当前架构

### 关键文件

| 文件 | 职责 |
|------|------|
| `src/voidx/ui/output/capture.py` | `OutputCapture` — agent 输出的统一捕获接口 |
| `src/voidx/ui/output/events/schema.py` | 事件类型定义（`MessageAppended`, `MarkdownAppended`, `ThoughtAppended` 等） |
| `src/voidx/ui/output/events/__init__.py` | `UiEventBus` + `DockEventConsumer` — 事件分发 |
| `src/voidx/ui/output/dock.py` | `BottomInputDock` — TUI 输出面板 |

### 当前实现模式

```python
class OutputCapture:
    def error(self, message: str) -> None:
        if via_events():
            ui_events.emit_direct(ErrorAppended(...))
            return
        # 非 event 模式：直接操作 tree
        self._tree.new_node(parent=self._parent, node_type="error", ...)
        dock.refresh()

    def print(self, *args, **kwargs) -> None:
        pass  # ← 非 event 模式下无输出

    def markdown(self, content: str) -> None:
        pass  # ← 非 event 模式下无输出

    def thinking(self, text: str) -> None:
        pass  # ← 非 event 模式下无输出

    def sep(self) -> None:
        pass  # ← 非 event 模式下无输出
```

### 非 event 模式下的可用工具

- `self._tree.new_node()` — 创建输出节点（支持 node_type, header, body_lines, collapsed 等）
- `self._parent` — 当前父节点
- `dock.refresh()` — 刷新 TUI 显示
- `rich.markdown.Markdown` — Markdown 渲染（已在 `events/__init__.py` 中导入使用）

## 设计

### 方案：补全 tree 操作实现

参照 `error()`/`warn()` 的模式，为每个方法添加非 event 模式的 tree 操作分支。

#### `print()`

```python
def print(self, *args, **kwargs) -> None:
    if via_events():
        text = " ".join(str(a) for a in args)
        ui_events.emit_direct(MessageAppended(agent_id=self._agent_id, text=text))
        return
    text = " ".join(str(a) for a in args)
    self._tree.new_node(
        parent=self._parent,
        node_type="message",
        header=escape(text),
        collapsed=False,
    )
    dock.refresh()
```

#### `markdown()`

```python
def markdown(self, content: str) -> None:
    if via_events():
        ui_events.emit_direct(MarkdownAppended(agent_id=self._agent_id, content=content))
        return
    self._tree.new_node(
        parent=self._parent,
        node_type="markdown",
        header=escape(content.split("\n")[0][:80]),
        body_lines=[escape(line) for line in content.split("\n")[:20]],
        collapsed=False,
    )
    dock.refresh()
```

注意：非 event 模式下无法使用 `rich.markdown.Markdown` 渲染（tree 节点只存文本），所以用纯文本 fallback。这与 `diff()` 方法的非 event 模式处理一致。

#### `thinking()`

```python
def thinking(self, text: str) -> None:
    if via_events():
        ui_events.emit_direct(ThoughtAppended(agent_id=self._agent_id, text=text))
        return
    self._tree.new_node(
        parent=self._parent,
        node_type="thinking",
        header=f"[dim]{escape(text[:200])}[/dim]",
        collapsed=True,
    )
    dock.refresh()
```

thinking 内容通常较长且不需要默认展开，所以 `collapsed=True`，截取前 200 字符作为 header。

#### `sep()`

```python
def sep(self) -> None:
    if via_events():
        ui_events.emit_direct(MessageAppended(agent_id=self._agent_id, text="─" * 40))
        return
    self._tree.new_node(
        parent=self._parent,
        node_type="sep",
        header="[dim]────────────────────────────────────────[/dim]",
        collapsed=False,
    )
    dock.refresh()
```

### 替代方案：废弃非 event 模式

如果非 event 模式（`via_events() == False`）是遗留路径且不再使用，可以：

1. 在每个 `pass` 方法中添加 `# non-event mode deprecated` 注释
2. 在 `via_events()` 返回 False 时记录 warning
3. 后续版本移除非 event 路径

**权衡**：当前无法确认非 event 模式是否还有活跃调用者。补全实现更安全，废弃需要更多调查。

## 实现计划

### Step 1: 补全四个方法

- `src/voidx/ui/output/capture.py`: 为 `print()`、`markdown()`、`thinking()`、`sep()` 添加非 event 模式的 tree 操作实现

### Step 2: 验证 node_type 注册

- 确认 `message`、`markdown`、`thinking`、`sep` 这些 node_type 在 tree 渲染中是否被识别
- 如果有 node_type 白名单或样式映射，需要添加对应条目

### Step 3: 测试

- 新增或更新测试，验证非 event 模式下四个方法能正确创建 tree 节点
- 验证 event 模式下行为不变

## 边界情况

| 场景 | 行为 |
|------|------|
| `print()` 无参数 | 创建空文本节点（与 event 模式下 `MessageAppended(text="")` 一致） |
| `markdown()` 内容含 ANSI 转义 | 非 event 模式下 `escape()` 处理，不渲染 Markdown |
| `thinking()` 内容为空 | 创建空 header 的折叠节点 |
| `sep()` 在 headless 模式下 | 仍然创建节点（headless 不显示但结构完整） |

## Non-goals

- 不改变 event 模式下的任何行为
- 不在非 event 模式下引入 Rich Markdown 渲染（tree 节点只存文本）
- 不移除或废弃非 event 模式
- 不修改 `via_events()` 的判断逻辑

## 验收标准

- [ ] `print()` 在非 event 模式下创建 `node_type="message"` 的 tree 节点
- [ ] `markdown()` 在非 event 模式下创建 `node_type="markdown"` 的 tree 节点
- [ ] `thinking()` 在非 event 模式下创建 `node_type="thinking"` 的折叠 tree 节点
- [ ] `sep()` 在非 event 模式下创建 `node_type="sep"` 的 tree 节点
- [ ] 四个方法在 event 模式下行为不变
- [ ] `error()` 和 `warn()` 的实现不受影响
- [ ] 新增测试覆盖非 event 模式下的四个方法
