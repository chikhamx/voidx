# 增量提交 Dock 子节点 - 技术设计文档

> **Status: Done**

## Context

TUI 的 committed lines 机制将输出内容分为两层：已提交到终端 scrollback 的行不再参与帧渲染，未提交的活跃行留在帧内持续刷新。

当前 `safe_flush_line_count()` 通过 `_is_node_chain_settled()` 判断哪些行可以安全提交。该方法要求节点自身及所有祖先都是 settled，否则该节点的行不会被提交。

实际运行中，`_current_agent` 节点在整个 LLM 处理轮次期间始终是 unsettled 状态。第一轮里它还会通过 `ensure_agent()` 创建一个可见的 `voidx` placeholder。这个 placeholder 本身长期未 settled，会阻塞后续已经完成的工具调用、工具结果和已结束的流式段落提交到 scrollback。

用户可见问题：

- 第一轮 agent 输出会先出现 `voidx` placeholder，之后它可能被真实流式文本复用或被 `_settle_stream_for_tool()` 回退成 placeholder。
- 已完成工具输出仍留在活跃帧内，长轮次时前面的内容不能稳定滚动查看。
- 如果简单把 `_is_node_chain_settled()` 改成只看节点自身，会绕开真实可变祖先，容易把仍会变化的可见行提交出去。

## Goals and Non-Goals

### Goals

- 去掉可见的 `voidx` placeholder。
- 让纯 agent/assistant 容器在渲染上透明，不产出可见 header 行。
- 让真实 AI 回复使用独立 stream 节点，并在 commit 后才 settled。
- 已完成工具、工具结果和文件变更在其可见前缀稳定后可以进入 committed lines。
- 保持“已提交内容不可变”的语义。

### Non-Goals

- 不改变 web/gateway 端渲染；当前问题来自 TUI committed lines。
- 不把仍在 streaming 的 AI 文本提前标记 settled。
- 不用固定行数阈值或强制 flush 绕过安全检查。

## Architecture

### 当前逻辑

```
safe_flush_line_count()
  -> 逐行扫描
  -> 每行查所属 node_id
  -> _is_node_chain_settled(node_id)
      -> 从 node 向上遍历到 root
      -> 任一祖先不在 _settled_node_ids -> 返回 False
```

当前树形关系：

```
root
├── turn (settled)
└── assistant / agent placeholder (unsettled, visible)
    ├── stream text (可能复用 placeholder 节点)
    ├── tool_call (完成后 settled)
    │   └── tool_result (settled)
    └── ...
```

会变化的情况：

- `set_stream()` 可能复用 placeholder 节点，把 header/body 改成真实回复。
- `_settle_stream_for_tool()` 可能把同一个节点重置回 placeholder，并清空 body。
- `finish_tool_node()` 会把 running 工具 header 改成完成态。
- `append_file_change()` 会复用工具节点并覆盖 header/body。
- status/subagent 节点在完成前也会更新 header、body 或状态。

### 方案

保留“提交必须基于稳定可见内容”的原则，但把不应该可见的容器从渲染中拿掉：

1. `ensure_agent()` 可以继续创建 `_current_agent` 作为逻辑容器，但该容器不显示 `voidx` placeholder。
2. `OutputTree` 对空 header 的 root-level assistant 容器透明渲染，只递归渲染它的子节点。
3. 默认 `set_stream()` 不再复用 `_current_agent` 容器本身，而是在容器下创建真实 stream 子节点。该 stream 节点未 commit 前保持 unsettled。
4. `_settle_stream_for_tool()` 不再把已有 stream 回退成 `voidx` placeholder。工具开始前只 commit 或移除当前 stream 节点。
5. 工具节点仍按现有生命周期：start 时 unsettled，finish/result/file change 完成后 settled。
6. `safe_flush_line_count()` 保持 prefix-only 扫描，但祖先链检查会忽略透明容器。透明容器不产出可见行，也不会作为未 settled 祖先卡住第一条可提交工具行。

## Data Model

无持久化数据模型变更。`_settled_node_ids` 仍表示节点内容不会再变化。变化点只在渲染和 stream 节点创建策略。

## Test Plan

- `ensure_agent()` 后渲染为空，不出现 `voidx` placeholder。
- 无前置 AI 文本时启动工具，渲染中直接出现工具行，不出现 `voidx` 父行。
- AI stream 后启动工具，不把 stream 行回退成 placeholder。
- 已完成工具和结果在透明容器下可以被 safe flush 覆盖。
- 仍在 streaming 的 AI 文本不会被 safe flush 提前提交。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 透明化 agent 容器 | 继续显示 placeholder；只检查节点自身 settled；跳过祖先继续扫兄弟节点 | 透明容器解决可见阻塞和 UI 噪音，同时保留保守提交语义 |
| stream 使用子节点 | 复用 `_current_agent` 本身 | 子节点让“容器生命周期”和“可见回复生命周期”分离，避免第一条 agent 消息被工具开始逻辑改写 |
| 不提前 settled streaming 文本 | commit 前标记 settled | streaming 文本仍会变化，提前提交会破坏 committed lines 不可变语义 |

## Open Questions

- [x] 是否需要同步修改 web gateway 端渲染？不需要，当前问题来自 TUI committed lines。
