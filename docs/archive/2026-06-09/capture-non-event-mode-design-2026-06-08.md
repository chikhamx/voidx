# Capture 非 Event 模式补全设计

> **Status: Done**

## 问题

`CaptureConsole` 的 `print()`、`markdown()`、`thinking()`、`sep()` 四个方法在非 event 模式下是空实现（`pass`），而同类的 `error()` 和 `warn()` 在两种模式下都有完整实现。这导致：

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
| `src/voidx/ui/output/capture.py` | `CaptureConsole` — subagent/headless 输出的统一捕获接口 |
| `src/voidx/ui/output/events/schema.py` | 事件类型定义（`MessageAppended`, `MarkdownAppended`, `ThoughtAppended` 等） |
| `src/voidx/ui/output/events/__init__.py` | `UiEventBus` + `DockEventConsumer` — 事件分发 |
| `src/voidx/ui/output/dock/app.py` | `BottomInputDock` — TUI 输出面板主体 |
| `src/voidx/ui/output/dock/nodes.py` | `DockNodeMixin` — `append_message()` / `append_ansi()` / `append_thought()` 等节点操作 |

### 当前实现模式

```python
class CaptureConsole:
    def error(self, message: str) -> None:
        if via_events():
            ui_events.emit_direct(ErrorAppended(...))
            return
        # 非 event 模式：已有 error/warn 直接操作 tree
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

- `self._parent` — 当前父节点
- `dock.append_message(..., parent=self._parent)` — 创建普通消息节点并处理 refresh / settled state
- `dock.capture(lambda console: console.print(...), parent=self._parent)` — 捕获 Rich 渲染后的 ANSI 文本
- `dock.append_thought(..., parent=self._parent)` — 创建现有 `node_type="thought"` 节点
- `rich.markdown.Markdown` / `rich.text.Text` — 已在 `VoidConsole` 非 event 路径中使用

原则：优先复用 `BottomInputDock` 的 append/capture API，不在 `CaptureConsole` 里复制 tree 节点细节。这样可以保持 ANSI、Markdown、settled state、refresh 行为与主 console 路径一致。

## 设计

### 方案：复用 dock append/capture API

参照 `VoidConsole` 的非 event 路径，为每个方法添加 fallback。非 event 模式下输出应挂到 `CaptureConsole` 的 `self._parent`，避免 subagent 输出跑到 root。

#### `print()`

```python
def print(self, *args, **kwargs) -> None:
    if via_events():
        # Event-mode behavior intentionally remains unchanged in this phase.
        return
    dock.capture(
        lambda console: console.print(*args, **kwargs),
        parent=self._parent,
    )
```

说明：本设计只补非 event fallback。event 模式下这几个方法保持现状，避免本期扩大事件语义或改变 subagent event 输出结构。非 event 模式使用 `dock.capture()`，保留 Rich markup 和 ANSI 样式。

#### `markdown()`

```python
def markdown(self, content: str) -> None:
    if via_events():
        return
    dock.capture(
        lambda console: console.print(Markdown(content)),
        parent=self._parent,
    )
```

注意：非 event 模式可以使用 `rich.markdown.Markdown` 渲染。`VoidConsole.markdown()` 当前已经通过 `dock.capture(lambda console: console.print(Markdown(content)))` 实现相同行为。

#### `thinking()`

```python
def thinking(self, text: str) -> None:
    if via_events():
        return
    dock.append_thought(text, parent=self._parent)
```

说明：使用现有 `node_type="thought"`，不新增 `thinking` 节点类型，避免渲染和协议层出现同义分叉。

#### `sep()`

```python
def sep(self) -> None:
    if via_events():
        return
    dock.append_message(
        "─" * self._dummy.width,
        style="dim",
        parent=self._parent,
    )
```

说明：不新增 `sep` 节点类型。分隔线作为 dim message 渲染，与 `VoidConsole.sep()` 的视觉行为一致。

### 替代方案：废弃非 event 模式

如果非 event 模式（`via_events() == False`）是遗留路径且不再使用，可以：

1. 在每个 `pass` 方法中添加 `# non-event mode deprecated` 注释
2. 在 `via_events()` 返回 False 时记录 warning
3. 后续版本移除非 event 路径

**权衡**：当前无法确认非 event 模式是否还有活跃调用者。补全实现更安全，废弃需要更多调查。

## 实现计划

### Step 1: 增加 parent-aware dock capture

- `src/voidx/ui/output/dock/app.py`: `BottomInputDock.capture()` 增加可选 `parent: OutputNode | None = None`
- `src/voidx/ui/output/dock/app.py`: `BottomInputDock.print()` 将 parent 透传给 `capture()`
- `capture()` 内部继续捕获 Rich 输出，再调用 `append_ansi(text, parent=parent)`

### Step 2: 补全四个方法

- `src/voidx/ui/output/capture.py`: 为 `print()`、`markdown()`、`thinking()`、`sep()` 添加非 event fallback
- `print()` / `markdown()` 通过 parent-aware `dock.capture()`
- `thinking()` 通过 `dock.append_thought(text, parent=self._parent)`
- `sep()` 通过 `dock.append_message(..., parent=self._parent, style="dim")`

### Step 3: 验证 node_type 注册

- 确认仅使用现有 `message` / `thought` 节点类型
- 不新增 `markdown`、`thinking`、`sep` 节点类型

### Step 4: 测试

- 新增或更新测试，验证非 event 模式下四个方法能正确创建 tree 节点
- 验证 event 模式下保持 no-op 行为
- 验证 subagent parent 下的非 event 输出不会挂到 root

## 边界情况

| 场景 | 行为 |
|------|------|
| `print()` 无参数 | 不创建节点；与 `dock.append_ansi()` 空白过滤一致 |
| `markdown()` 内容含 ANSI 转义 | 通过 Rich Markdown 渲染后捕获 ANSI，保持和 `VoidConsole.markdown()` 一致 |
| `thinking()` 内容为空 | 不创建节点；与 `dock.append_thought()` 空白过滤一致 |
| `sep()` 在 headless/subagent capture 下 | 创建 dim message 节点，挂到 `self._parent` |

## Non-goals

- 不改变 event 模式下的任何行为
- 不新增 `markdown`、`thinking`、`sep` 节点类型
- 不移除或废弃非 event 模式
- 不修改 `via_events()` 的判断逻辑

## 验收标准

- [x] `print()` 在非 event 模式下创建 `node_type="message"` 的 tree 节点
- [x] `markdown()` 在非 event 模式下通过 Rich Markdown capture 创建 message 节点
- [x] `thinking()` 在非 event 模式下创建现有 `node_type="thought"` 节点
- [x] `sep()` 在非 event 模式下创建 dim message 节点
- [x] 非 event 输出挂到 `CaptureConsole` 的 `self._parent`
- [x] 四个方法在 event 模式下保持 no-op 行为
- [x] `error()` 和 `warn()` 的实现不受影响
- [x] 新增测试覆盖非 event 模式下的四个方法
