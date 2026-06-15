# 增量提交已完成的 Dock 子节点 — 技术设计文档

## Context

TUI 的已提交行（committed lines）机制将输出内容分为两层：已提交到终端 scrollback 的行不再参与帧渲染，未提交的活跃行留在帧内持续刷新。

当前 `safe_flush_line_count()` 通过 `_is_node_chain_settled()` 判断哪些行可以安全提交。该方法要求**节点自身及所有祖先都是 settled**，否则该节点的行不会被提交。

实际运行中，`_current_agent` 节点在整个 LLM 处理轮次期间始终是 unsettled 状态（从 `ensure_agent()` 创建到 `commit_stream()` 才标记 settled）。由于 `_current_agent` 是所有输出内容的父节点，它的 unsettled 状态阻塞了整棵子树的提交，包括已经完成的工具调用结果、已结束的流式段落等。

**用户看到的现象**：一整轮对话的所有内容都留在帧内，直到轮次结束才一次性提交到 scrollback。长轮次时前面的内容无法滚动查看。

## Goals and Non-Goals

### Goals

- 已 settled 的子节点在祖先 unsettled 时也能被提交到 scrollback
- 保持"已提交内容不可变"的语义：只有自身内容不会再变化的节点才被提交
- 不改变 dock 的节点树结构和 settled 标记时机

### Non-Goals

- 不改变流式输出节点（stream_node）的 settled 时机（它确实还在变化）
- 不改变帧渲染的差分逻辑
- 不处理 web/gateway 端的渲染（它没有 committed lines 机制）

## Architecture

### 当前逻辑

```
safe_flush_line_count()
  → 逐行扫描
  → 每行查所属 node_id
  → _is_node_chain_settled(node_id)
      → 从 node 向上遍历到 root
      → 任一祖先不在 _settled_node_ids → 返回 False → 阻断提交
```

节点树结构：

```
root
├── turn (settled)
└── agent (unsettled ← 整轮期间)
    ├── stream_node (unsettled)
    ├── tool_call (settled after finish_tool)
    │   └── tool_result (settled)
    ├── stream_node (unsettled)
    ├── tool_call (settled)
    │   └── tool_result (settled)
    └── ...
```

### 改动方案

将 `_is_node_chain_settled` 改为只检查节点自身是否 settled，不检查祖先链。

**理由**：子节点 settled 意味着它的内容（header、body_lines）不会再变化。祖先是否还在接收新子节点，不影响已有子节点内容的稳定性。已提交到 scrollback 的行不会被重新渲染，所以只要子节点自身内容不变，提交就是安全的。

**验证**：检查了所有对 `node.header`、`node.body_lines` 的赋值操作，settled 的子节点（tool_result、finish_tool 后的 tool_call、append_message 创建的节点等）在标记 settled 后不会再被修改。

### 改动点

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/output/dock/app.py` | `_is_node_chain_settled` → `_is_node_settled`，只检查节点自身 |

改动极小，核心就是将：

```python
def _is_node_chain_settled(self, node_id: str) -> bool:
    node = self._tree.get(node_id)
    while node is not None and node is not self._tree.root:
        if node.id not in self._settled_node_ids:
            return False
        node = node.parent
    return True
```

改为：

```python
def _is_node_settled(self, node_id: str) -> bool:
    return node_id in self._settled_node_ids
```

同时更新 `safe_flush_line_count` 中的调用。

## Data Model

无数据模型变更。`_settled_node_ids` 的语义不变，只是查询时不再向上传播。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| unsettled 子节点被误提交 | 不会发生：只有 `_settled_node_ids` 中的节点才返回 True |
| 祖先 header 变化导致已提交行与帧内行不一致 | 不影响：已提交行在 scrollback 中，帧内只渲染未提交的活跃行，两者不重叠 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 只检查节点自身 settled | 1. 只检查自身；2. 跳过 unsettled 祖先继续扫描兄弟节点；3. 基于行数阈值强制提交 | 方案 1 最简单且语义正确；方案 2 改动更大但效果相同；方案 3 破坏"已提交不可变"语义 |

## Open Questions

- [ ] 是否需要同步修改 web gateway 端的渲染逻辑？（当前 gateway 没有 committed lines 机制，不需要）
