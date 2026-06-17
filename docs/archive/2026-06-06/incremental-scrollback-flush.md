# 增量提交方案：Incremental Scrollback Flush

## 问题

当前 `_flush_committed` 只在 **busy→idle** 时真正提交内容。一整轮对话的用户输入、工具调用、工具结果、LLM 输出都会留在 live frame 里反复重绘，直到回合结束才一次性写入 scrollback。

这会带来几个问题：

1. live frame 随回合增长而持续变大。
2. `_render_frame` 每次刷新都重新捕获、写入、覆盖大量已稳定内容。
3. TTY 下重绘量大，容易造成闪烁和性能浪费。
4. 非 TTY 下中间结果不能及时出现，必须等整轮结束。

目标是：**scrollback 里只写入之后绝不会再变的内容；live frame 只保留第一个可变节点之后的尾部。**

## 设计原则

一旦内容写入终端 scrollback，就不能再修改。因此 flush 边界必须满足：

- 行本身所属节点已经稳定。
- 该行所有祖先节点也已经稳定。
- 该行之前没有尚未稳定的节点阻断前缀。

这意味着不能简单维护一个全局 `_settled_line_count`。tree 是嵌套结构，子节点稳定不代表父节点稳定；并发工具和 subagent 也可能让后续事件修改更早出现的父节点。

## 推荐方案

在 dock 侧维护 **节点级 settled 状态**，在 TUI flush 时计算 **可安全提交的最长前缀**。

### 1. Dock 维护 settled 节点集合

在 `BottomInputDock` 中新增：

```python
self._settled_node_ids: set[str] = set()
```

提供几个内部方法：

```python
def _mark_settled(self, node: OutputNode | None) -> None:
    if node is not None:
        self._settled_node_ids.add(node.id)

def _mark_unsettled(self, node: OutputNode | None) -> None:
    if node is not None:
        self._settled_node_ids.discard(node.id)

def _is_node_chain_settled(self, node_id: str) -> bool:
    node = self._tree.get(node_id)
    while node is not None and node is not self._tree.root:
        if node.id not in self._settled_node_ids:
            return False
        node = node.parent
    return True
```

`reset()` 和 `restore_tree()` 需要清空 settled 集合。恢复历史 tree 时，如果它代表已经落盘的 transcript，可以选择全部标记 settled；如果只是运行期恢复，则保守清空。

### 2. Tree 提供行到节点的映射

已有 `OutputTree.render_with_line_map(width)` 可以返回：

```python
lines, line_map = tree.render_with_line_map(width)
```

flush 边界计算复用这套 line map。不可点击的 body line 目前不一定全部有映射，因此需要补强 `_walk_render`：每一条 header/body/collapsed 行都写入所属 node id。点击逻辑如果仍只想响应可点击节点，可以另保留 click map，或让 click 层自行过滤 node type。

### 3. Dock 计算安全 flush 边界

新增：

```python
def safe_flush_line_count(self, width: int, committed: int) -> int:
    lines, line_map = self._tree.render_with_line_map(width)
    limit = len(lines)
    index = committed
    while index < limit:
        node_id = line_map.get(index)
        if node_id is None or not self._is_node_chain_settled(node_id):
            break
        index += 1
    return index
```

这个方法只返回从 `committed` 开始连续可提交的前缀。即使后面有一个节点已经 settled，只要前面仍有 running 节点，就不会越过它提前写 scrollback。

### 4. TUI 使用 safe flush limit

`_flush_committed()` 去掉“只有 busy→idle 才能提交”的门控，但保留 busy→idle 的最终兜底：

```python
width = self._frame_width()
tree_lines = dock.tree.render(width)

if force:
    flush_limit = len(tree_lines)
elif self._was_busy and not self._busy:
    flush_limit = len(tree_lines)
else:
    flush_limit = dock.safe_flush_line_count(width, self._committed_line_count)

if flush_limit <= self._committed_line_count:
    return

flush_lines = tree_lines[self._committed_line_count:flush_limit]
self._committed_line_count = flush_limit
```

后续 TTY / 非 TTY 打印逻辑保持不变。busy 期间只提交 safe prefix；busy→idle 时提交剩余内容，保证保守未 settled 的节点不会卡在 frame 里。

`invalidate()` 当前已经每次调用 `_flush_committed()`，无需额外改动入口；真正要改的是 `_flush_committed()` 内部的 flush limit 计算。

## 固化规则

| 事件/方法 | settled 行为 | 说明 |
|---|---|---|
| `start_turn()` | mark turn settled | 用户输入节点创建后不会再变 |
| `set_stream()` | mark stream node unsettled | 流式内容仍会变化 |
| `commit_stream()` | mark non-current-agent stream node settled | 子 stream 完成；root/current agent stream 仍可能追加 tool child，保守等待后续边界或 busy→idle 兜底 |
| `discard_stream()` | 移除节点或保持 unsettled | 不应写入被丢弃内容 |
| `start_tool()` | mark tool unsettled | 工具 header 仍会变为 done/error |
| `finish_tool_node()` | 不单独 mark settled | header 会变稳定，但后续可能插入 result/diff child，不能提前提交该 subtree |
| `append_tool_result()` | mark result 和 parent tool subtree settled | 工具结果创建后，tool subtree 才可成为安全前缀的一部分 |
| `append_file_change()` | mark diff/tool subtree settled | 文件变更会更新 tool 节点本身，必须等更新完成后再 settled |
| `set_status()` | mark status unsettled | status 会持续更新或移除 |
| `finish_status(remove=True)` | 移除 status | 不进入 scrollback |
| `finish_status(remove=False)` | mark status settled | 折叠后的状态节点稳定 |
| `append_message()` / `append_ansi()` / `append_error()` / `append_thought()` | mark created node settled | 创建后不再更新 |
| `SubagentStarted` | mark subagent unsettled | header、children、progress 仍会变化 |
| `SubagentFinished` | mark subagent settled | 更新 header、清理 progress 后才稳定 |

注意：`finish_tool_node()` 不能作为“工具节点及子节点全部固化”的信号。当前真实事件顺序是 `ToolFinished` 后才追加 `FileChangeAppended` 或 `ToolResultAppended`；而 `FileChangeAppended` 还会更新 tool 节点本身。因此工具 subtree 的 settled 应该在 result/diff 追加完成后推进。

## 优化后流程

普通 assistant：

```text
用户输入创建 turn → turn settled → flush(turn)
LLM stream 更新 → stream unsettled → 留在 frame
LLM commit → child stream 可 settled；root/current agent stream 保守等待 busy→idle 或后续 agent 边界
```

工具调用：

```text
start_tool → tool unsettled → 留在 frame
finish_tool_node → tool header 更新，但 subtree 仍 unsettled
append_tool_result / append_file_change → tool subtree settled
如果 tool 及祖先都 settled，safe prefix 推进并 flush
```

subagent：

```text
SubagentStarted → subagent unsettled
子 stream commit → stream node settled，但祖先 subagent 未 settled，不能 flush
SubagentFinished → subagent header 更新并 settled
safe prefix 才能越过该 subtree
```

## 风险与应对

| 风险 | 应对 |
|---|---|
| body line 没有 node id | 扩展 render line map，让每条渲染行都有 owning node |
| 点击映射和 line map 职责混淆 | 拆成 `render_with_line_map` 和 click 过滤，或保持 click 层按 node type 过滤 |
| 子节点 settled 但父节点之后会变 | `_is_node_chain_settled()` 检查所有祖先 |
| 工具 finish 早于结果追加 | tool subtree 等 `append_tool_result()` / `append_file_change()` 后 settled |
| subagent stream 完成但 subagent header 后续改变 | `SubagentFinished` 后才 mark subagent settled |
| resize 后行号变化 | 每次按当前 width 重新 render 并重新计算 safe prefix，不缓存行数 |
| running status 阻断后续稳定节点 | 这是正确行为；scrollback 只能提交连续安全前缀 |
| flush 频率增加 | flush 前已有 no-op 检查；实际提交量减少，frame 变小 |
| 保守节点未被 marked settled | busy→idle 兜底 flush 提交剩余内容，保持原有最终输出语义 |

## 测试计划

新增或调整 `tests/test_pure_tui.py` 与 `tests/test_ui_events.py`：

1. `start_turn()` 后普通 turn 可以提前 flush。
2. `set_stream()` 期间不 flush stream，`commit_stream()` 后 flush。
3. `ToolFinished` 后、`ToolResultAppended` 前不丢内容；结果追加后完整 flush。
4. `FileChangeAppended` 更新 tool node 后可以 flush diff 内容。
5. subagent child stream commit 后，因为祖先 subagent 未完成，不能 flush subtree。
6. `SubagentFinished` 后 subtree 可以 flush，且 scrollback 中是 completed/failed header。
7. `finish_status(remove=True)` 不输出 status；`remove=False` 输出折叠完成状态。
8. 非 TTY 下已 settled 前缀能提前输出，不包含 input chrome。
9. resize 后不会因旧行数重复或跳过内容。

## 涉及文件

| 文件 | 改动 |
|---|---|
| `src/voidx/ui/output/dock/app.py` | 增加 settled 集合、safe flush 计算、reset/restore 处理 |
| `src/voidx/ui/output/dock/nodes.py` | 在节点创建/完成/移除路径标记 settled/unsettled |
| `src/voidx/ui/output/events/__init__.py` | 在 subagent 完成等直接改 node 的路径标记 settled |
| `src/voidx/ui/output/tree.py` | 确保 line map 覆盖每条渲染行 |
| `src/voidx/ui/tui/app.py` | `_flush_committed` 改用 `safe_flush_line_count` |
| `tests/test_pure_tui.py` | 覆盖 flush 边界和 TTY/非 TTY 行为 |
| `tests/test_ui_events.py` | 覆盖工具、stream、subagent 的事件顺序 |

## 不改动的部分

- `_render_frame` 的 cursor positioning 与 frame 覆盖逻辑。
- `_render_input_region` 的局部刷新逻辑。
- `_make_room_for_frame` 的滚动计算。
- UI event schema。
- tree 的视觉渲染样式。
