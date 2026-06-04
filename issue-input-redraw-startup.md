# 问题分析：输入框每输入一个字符就重绘启动信息

## 现象

在 voidx TUI 中，用户在输入框每输入一个字符，启动信息（startup banner）就会被完整重绘一次。

## 根因

PureTui 采用**全量帧渲染**策略——每次按键都重新绘制整个终端画面，包括上方不变的 transcript 区域（含 startup banner）。

### 完整调用链

```
用户按键
  → _read_input_raw()                    # 读取原始字节
  → _process_input(data)                 # 解析按键
  → _insert_text(ch)                     # 修改输入状态
  → 返回 needs_render = True
  → _render_frame()                      # ★ 全量帧渲染入口
      → _render_impl(height=...)         # 构建 Rich renderable
          → dock.tree.render(width)      # 获取所有 tree 行（含 startup）
          → visible_tree = tree_lines[-body_limit:]  # 取尾部可见行
          → 逐行转为 Rich Text 对象
      → cap.print(renderable)            # Rich Console 序列化为 ANSI 字符串
      → stdout.write("\x1b[J")           # 清除从光标到屏幕底部
      → stdout.write(ansi)               # 写入整个帧（含 startup）
```

### 关键代码位置

**1. 每次按键都触发全量渲染** — `src/voidx/ui/tui.py:204-205`

```python
if self._process_input(data):
    self._render_frame()
```

`_process_input` 对任何可打印字符都返回 `True`，无条件触发 `_render_frame()`。

**2. 全量帧渲染无差分** — `src/voidx/ui/tui.py:370-403`

```python
def _render_frame(self) -> None:
    ...
    renderable = self._render_impl(height=term_height)  # 构建完整画面
    ...
    cap.print(renderable)       # 序列化完整画面为 ANSI
    ansi = buf.getvalue()
    ...
    sys.stdout.write(f"\x1b[{start_row};1H")  # 光标移到帧起始
    sys.stdout.write("\x1b[J")                 # 清除到屏幕底部
    sys.stdout.write(ansi)                     # 写入完整帧
```

每帧都执行"定位→清屏→全量写入"，没有任何差分或局部更新逻辑。

**3. _render_impl 每次重建所有可见行** — `src/voidx/ui/tui.py:1246-1283`

```python
tree_lines = dock.tree.render(width)       # 获取全部 tree 行
visible_tree = tree_lines[-body_limit:]    # 取可见尾部
for line in visible_tree:
    elements.append(_text_from_line(line))  # 逐行转 Rich Text
```

即使 tree.render() 有缓存（`_cached_lines`），每帧仍要把所有可见行重新转为 Rich Text 对象，再通过 Console 序列化为 ANSI。

**4. tree.render() 的缓存仅避免字符串计算** — `src/voidx/ui/tree.py:175-189`

```python
def render(self, console_width: int = 80) -> list[str]:
    if not self._dirty and not self._dirty_nodes and self._cached_width == console_width:
        return self._cached_lines  # 缓存命中，跳过树遍历
```

缓存只避免了树遍历和字符串拼接，但 `_render_impl` 后续的 Rich Text 构造和 Console 序列化仍然每帧执行。

### 为什么看起来是"启动信息被重绘"

- startup banner 是 tree 中的第一个节点，占据可见区域顶部
- 每帧全量写入时，startup 内容被完整写入终端
- 用户视觉上看到 startup 区域闪烁/重绘，误以为是 startup 本身被重新触发
- 实际上 startup 数据没有重新生成，只是被重新序列化和写入

## 影响范围

- **性能**：每帧 Rich Console 序列化 + 全量 ANSI 写入，输入时产生不必要的 CPU 和 I/O 开销
- **视觉**：在慢终端或远程 SSH 下可能出现可见闪烁
- **体验**：用户感知到"每输入一个字符整个画面都刷新"

## 优化方向

### 方案 A：输入区局部刷新（推荐，改动最小）

当只有输入框内容变化时（无 tree 变更、无 status 变更），只重绘输入区域，跳过 transcript 区域。

实现思路：
- 在 `_render_frame` 中判断本次渲染是否仅由输入变化触发
- 如果是，用 ANSI 定位序列只更新输入框区域（分隔线以下）
- transcript 区域保持不变

改动位置：`PureTui._render_frame()`

### 方案 B：帧差分渲染

缓存上一帧的 ANSI 输出，对比差异，只写入变化的部分。

实现思路：
- 维护 `_last_frame_ansi: str`
- 新帧与旧帧按行对比，只对差异行执行定位+写入
- 可参考 Rich Live 的内部实现

改动位置：`PureTui._render_frame()`

### 方案 C：利用 tree 增量渲染信息

`OutputTree.render()` 已支持增量渲染（`_incremental_render`），但返回值没有携带"哪些行变化了"的信息。

实现思路：
- 让 `render()` 返回变更行范围
- `_render_impl` 只重建变更区域的 Rich Text
- `_render_frame` 只写入变更区域

改动位置：`OutputTree.render()` + `PureTui._render_impl()` + `PureTui._render_frame()`

### 方案对比

| 方案 | 改动范围 | 效果 | 复杂度 | 风险 |
|------|---------|------|--------|------|
| A 输入区局部刷新 | tui.py | 输入时不再重绘 transcript | 低 | 低 |
| B 帧差分渲染 | tui.py | 所有场景减少 I/O | 中 | 中（差分算法边界情况） |
| C tree 增量信息 | tree.py + tui.py | 精确更新变更区域 | 高 | 高（跨模块协议变更） |

**建议先实施方案 A**，解决最常见的"输入时重绘启动信息"问题，后续再考虑方案 B/C。
