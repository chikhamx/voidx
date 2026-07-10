> **Status: Done** — Archived on 2026-07-10.

---
name: guidance-rendering
display_name: /guide 消息渲染方案
description: 让 /guide 发送的 guidance 消息在 TUI 和 Web 端像普通用户消息一样可见渲染
doc_type: tech-design
audience: human+llm
---

# /guide 消息渲染 — 技术设计文档

## TL;DR

`/guide` 发送的 guidance 消息当前在 TUI 和 Web 端都没有可见渲染，用户无法确认消息是否发送成功或被 LLM 读取。方案核心：在 `submit_guidance()` 中区分用户发起 vs 系统内部 guard guidance，仅对用户发起的 guidance **新增 emit `MessageAppended(style="guidance")` 事件**（此前事件类型仅定义、从未被 emit），复用两端已就绪的 consumer 实现可见渲染。

## Context

### 当前行为

`/guide` 的完整调用链（已验证）：

1. **命令注册**：`/guide` 在命令列表声明为 "Add guidance to the running agent turn"，catalog 标记 `requiresArgs`：`src/voidx/ui/commands.py:25`、`src/voidx/ui/command_catalog.py:98`
2. **Slash 分发**：`SlashHandler.dispatch()` 路由到 `self._guide(args)`：`src/voidx/agent/slash/handler.py:96`
3. **参数校验**：`SlashGuideMixin._guide()` trim 文本，空文本打印 usage，调用 `host.submit_guidance()`：`src/voidx/agent/slash/guide.py:8-18`
4. **Host 适配**：`SlashHostAdapter.submit_guidance()` 委托到 graph 的 `submit_guidance()`：`src/voidx/agent/slash/host.py:219-221`
5. **入队 + 事件**：`VoidXGraph.submit_guidance()` 压缩空白、截断到 2000 字符、append 到 `_pending_guidance`，发 `GuidanceSubmitted` 事件：`src/voidx/agent/graph/core/voidx_graph.py:402-413`
6. **LLM 注入**：`_drain_pending_guidance()` 把 pending guidance 转成 `HumanMessage(additional_kwargs={GUIDANCE_MARKER: True})`：`src/voidx/agent/graph/core/voidx_graph.py:415-423`
7. **LLM 调用**：guidance message 进入 `llm_messages`，传给 `_stream_llm()`：`src/voidx/agent/graph/core/llm.py:185-190, 307`

### 渲染现状

| 端 | GuidanceSubmitted 事件 | 渲染结果 |
|----|----------------------|---------|
| TUI | `DockEventConsumer` 直接 `return None` | **不渲染** |
| Web | `GatewayEventConsumer` → adapter `_on_guidance()` → `style: "guidance"` message item | **会渲染 guidance 事件；新增 `MessageAppended` 后必须避免重复渲染** |

### 问题来源

- TUI：`_submit_guidance_bypass()` 只调 external handler，无本地回显：`tui/voidx_cli/app.py:485-499`
- Web：running 时提交只加按钮动画，不追加消息气泡：`frontend/src/main.ts:773-786`
- 用户输入 `/guide` 后界面毫无反馈，无法确认发送成功或被 LLM 读取

### 关键约束：guard guidance

`_submit_guard_guidance()` 也调用 `submit_guidance()`，但 guard guidance 是系统内部生成的（工具失败、重复工具、无进展等），**不应渲染为用户可见消息**：`src/voidx/agent/graph/tool_executor/guards.py:141-146`

调用路径：
- 用户发起：`/guide` → `SlashHandler._guide()` → `host.submit_guidance()` → `VoidXGraph.submit_guidance()`
- 系统内部：`_submit_guard_guidance()` → `host.submit_guidance()` → `VoidXGraph.submit_guidance()`

两者共用同一个 `submit_guidance()` 入口，方案必须区分来源。

## Goals / Non-Goals

### Goals

- 用户通过 `/guide` 发送的消息在 TUI 和 Web 端都有可见渲染
- 渲染样式与普通用户消息一致，不做特殊视觉区分
- guard guidance 不受影响，继续不渲染
- 复用现有 `MessageAppended` 事件链路，最小改动

### Non-Goals

- 不改变 guidance 注入 LLM 的机制（`_drain_pending_guidance()` + `GUIDANCE_MARKER` 不变）
- 不改变 `GuidanceSubmitted` 事件的语义（保留用于内部状态追踪）
- 不改变 guidance 的截断/丢弃逻辑（2000 字符限制、turn 结束时清空）
- 不给 guard guidance 加渲染

## Proposed Design

### 核心思路

在 `submit_guidance()` 增加 `source` 参数区分来源。用户发起的 guidance（`source="user"`）额外发 `MessageAppended(style="guidance")` 事件，并把它作为 TUI/Web 可见渲染的唯一来源；系统内部 guard guidance（`source="guard"`）不发渲染事件，保持隐藏。`GuidanceSubmitted` 继续用于内部状态追踪，但不再驱动 Web 可见消息渲染，避免与 `MessageAppended` 重复。

### Request / Data Flow

**用户发起的 guidance（`/guide`）**：

1. 用户输入 `/guide <text>` → TUI `_submit_guidance_bypass()` 或 Web gateway `{"kind": "guide"}`
2. → `run_loop._handle_web_command()` → `submit_guidance(text, source="user")`
3. → `VoidXGraph.submit_guidance()`：空白压缩 + 截断，入队 `_pending_guidance`，发 `MessageAppended(style="guidance")` + `GuidanceSubmitted`
4. → `DockEventConsumer` 收到 `MessageAppended` → `dock.append_message(display_text, style="guidance")` → TUI 渲染
5. → `GatewayEventConsumer` 收到 `MessageAppended` → adapter `_on_message()` → Web 前端渲染
6. → `GuidanceSubmitted` 仍发出，但 gateway 不再把它转成可见 message item
7. 下一次 LLM 调用：`_drain_pending_guidance()` → `HumanMessage(GUIDANCE_MARKER=True)` → 注入 `llm_messages`

**系统内部 guard guidance**：

1. `_submit_guard_guidance()` → `submit_guidance(text, source="guard")`
2. → `VoidXGraph.submit_guidance()`：入队 `_pending_guidance`，**不发 `MessageAppended`**，只发 `GuidanceSubmitted`
3. 下一次 LLM 调用：同上注入

### API / Function Contract

| Name | Input | Output | Error Behavior |
|------|-------|--------|----------------|
| `VoidXGraph.submit_guidance` | `text: str`, `source: Literal["user", "guard"] = "user"` | `bool` | 空文本返回 `False`；超长截断并标记 `truncated` |
| `SlashHostAdapter.submit_guidance` | `text: str` | `bool` | 委托到 graph，`source` 默认 `"user"` |
| `SlashCommandHost.submit_guidance` (Protocol) | `text: str` | `bool` | 接口签名不变 |
| `_submit_guard_guidance` | `host`, `guidance: GuardGuidance \| None` | `None` | 调用 `submit_guidance(message, source="guard")` |

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| 用 `source` 参数区分来源 | 两个独立方法 `submit_user_guidance()` / `submit_guard_guidance()` | `source` 参数改动最小，不破坏 Protocol 接口，guard 调用点只改一行 |
| 复用 `MessageAppended` 事件 | 继续让 `GuidanceSubmitted` 直接渲染 | `MessageAppended` 两端 consumer 实现已就绪（TUI DockEventConsumer: consumers.py:189-190, Web GatewayEventConsumer adapter: adapter.py:212-215），作为唯一可见渲染事件可避免 Web 双气泡 |
| TUI 不做本地即时回显 | TUI `_submit_guidance_bypass()` 里直接 `dock.append_message()` | `submit_guidance()` 内部调 `emit_direct` 同步调用 consumer.handle，无需依赖异步 events bus queue，`MessageAppended` 会被 `DockEventConsumer` 即时消费渲染，无需本地回显 |
| Web running 时不做本地 append | 前端 running 分支本地即时追加 guidance 气泡 | 后端 `MessageAppended` 通过 gateway adapter 即时推送；本地即时追加会和后端事件形成双气泡，且对重复 guidance、截断文本和竞态不稳 |
| `GuidanceSubmitted` 事件保留但不渲染 | 删除它，只用 `MessageAppended` | `GuidanceSubmitted` 带 `truncated` 字段，用于内部状态追踪；Web 可见渲染统一由 `MessageAppended` 承担 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| guard guidance 调用点漏传 `source="guard"` | 系统内部 guidance 被渲染为用户消息，干扰用户 | 只有一个调用点 `_submit_guard_guidance()`，改动可控；加测试覆盖 |
| `MessageAppended` 在 TUI 和后端事件之间重复渲染 | 用户看到两条相同消息 | TUI 不做本地回显，完全依赖后端事件，不会重复 |
| `GuidanceSubmitted` 和 `MessageAppended` 都发，前端处理两次 | 前端可能渲染两次 | gateway adapter 不再把 `GuidanceSubmitted` 转成可见 message item；Web running 分支也不做本地 append，确保只有 `MessageAppended` 负责渲染 |
| 截断的 guidance 渲染时丢失 `truncated` 标记 | 用户不知道消息被截断 | 在 `submit_guidance()` 内分离 `guidance_text` 与 `display_text`：`guidance_text` 入队给 LLM，`display_text` 仅用于 `MessageAppended`，截断时追加 `[truncated]` |
| `_submit_guard_guidance` 用 `getattr(host, "submit_guidance")` 兼容不同 host 类型 | 如果某 host 实现了 `submit_guidance(self, text)` 但不接受 `source` 参数，会引发 `TypeError` | 已排查：目前所有 host 都是 `VoidXGraph` 实例，签名会加 `source`。防御措施：让 `SlashHostAdapter.submit_guidance` 加 `**kwargs` 兜底，确保接口向前兼容 |

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `src/voidx/agent/graph/core/voidx_graph.py` | `submit_guidance()` 加 `source` 参数，`source="user"` 时发 `MessageAppended` | 核心改动 |
| `src/voidx/agent/graph/contracts.py` | `_pending_guidance` 类型注释不变 | 仅参考 |
| `src/voidx/agent/slash/host.py` | `submit_guidance()` 适配层透传 `source` | Protocol 签名可选参数，默认 `"user"`；加 `**kwargs` 兜底确保与 `getattr(host, "submit_guidance")` 的防御性调用兼容 |
| `src/voidx/agent/graph/tool_executor/guards.py` | `_submit_guard_guidance()` 调用时传 `source="guard"` | 一行改动 |
| `src/voidx/agent/graph/run_loop.py` | `_handle_web_command()` 调用 `submit_guidance()` 时传 `source="user"` | 显式传参 |
| `frontend/src/main.ts` | running 时提交不做本地 `appendMessageItem()`；保留 pending 按钮状态，等待后端 `MessageAppended` 推送 | 前端改动 |
| `frontend/styles.css` | 无需改动，`.message-guidance` 已与 `.message-markdown` 样式一致 | 不做视觉区分 |

### Existing Behavior

- `submit_guidance()` 签名是 `submit_guidance(self, text: str) -> bool`：`src/voidx/agent/graph/core/voidx_graph.py:402`
- `submit_guidance()` 发 `GuidanceSubmitted` 事件：`src/voidx/agent/graph/core/voidx_graph.py:411-412`
- `DockEventConsumer` 对 `GuidanceSubmitted` 直接 `return None`：`src/voidx/ui/output/events/consumers.py:199-200`
- `GatewayEventConsumer` adapter `_on_guidance()` 当前会转 `style: "guidance"` message item：`src/voidx/ui/gateway/adapter.py:243-247`；实现时需要停止把 `GuidanceSubmitted` 渲染为可见 message item，避免和 `MessageAppended` 双气泡
- `DockEventConsumer` 对 `MessageAppended` 调 `dock.append_message(text, style=style)`：`src/voidx/ui/output/events/consumers.py:189-190`
- TUI 模式下 `via_events()` 为 true，但 `submit_guidance()` 发 `MessageAppended` 用的是 `emit_direct()`（同步调用 consumer.handle），不经过异步 events bus queue，即时到达 DockEventConsumer
- TUI `_submit_guidance_bypass()` 走 external handler → `run_loop._handle_web_command()` → `submit_guidance()`，不经 events bus，但 `submit_guidance()` 中的 `emit_direct` 直接触达 consumer
- `MessageAppended` 事件**此前从未被 emit**（schema 中已定义、consumer 已实现模式匹配）。本 spec 首次引入 emit 调用点：`src/voidx/agent/graph/core/voidx_graph.py:412`（加在 `GuidanceSubmitted` emit 之前）
- `MessageAppended(text=guidance, ...)` 中的 `guidance` 是经过 `_process_guidance()` 处理后的文本：空白压缩 → 2000 字符截断 → 用于事件。处理后的文本与入队到 `_pending_guidance` 的一致
- Web 前端 running 时提交只加按钮动画，不追加消息：`frontend/src/main.ts:773-786`
- Web 前端 `appendMessageItem()` 对 `style="guidance"` 用 markdown 渲染：`frontend/src/render.ts:245`
- `.message-guidance` CSS 当前与 `.message-markdown` 完全一样：`frontend/styles.css:79-84`
- turn 结束时 pending guidance 被清空，有 `WarningAppended` 提示：`src/voidx/agent/graph/turn_runner.py:435-443`

### Target Behavior

- `submit_guidance(text, source="user")` 时：先空白压缩 + 截断 → 处理后的文本同时用于 `_pending_guidance` 入队和事件，**先发 `MessageAppended(style="guidance")`，后发 `GuidanceSubmitted`**（顺序依赖：`DockEventConsumer` 消费 `GuidanceSubmitted` 时 `return None` 不做渲染，所以 `MessageAppended` 必须先发确保渲染赶上）
- `submit_guidance(text, source="user")` 时，如果 `emit_direct(MessageAppended(...))` 返回 `False`（events bus 未运行），**不入队、不发 `GuidanceSubmitted`、返回 `False`**——让调用方知道发送失败并给用户报错，而不是静默入队成为幽灵消息
- `submit_guidance(text, source="guard")` 时，不发 `MessageAppended`，只发 `GuidanceSubmitted`（保持现状）；guard guidance 不受 `emit_direct` 失败影响，继续走入队逻辑，因为 guard 是系统内部行为不需要用户反馈
- `submit_guidance(text, source="guard")` 时，不发 `MessageAppended`，只发 `GuidanceSubmitted`（保持现状）

- TUI：`MessageAppended` 被 `DockEventConsumer` 消费 → `dock.append_message(text, style="guidance")` → 渲染为 guidance 样式消息节点
- Web：`MessageAppended` 被 `GatewayEventConsumer` 推送 → 前端 `appendMessageItem()` 渲染 guidance 气泡
- Web 前端 running 时提交：不本地 `appendMessageItem()`，只显示 pending 按钮状态；后端 `MessageAppended` 经 gateway adapter 即时推送并渲染 guidance 气泡
- guidance 消息样式与普通用户消息一致，不做特殊视觉区分

### Invariants

- `GUIDANCE_MARKER` (`_voidx_guidance`) 标记机制不变：`src/voidx/llm/message_markers.py:6`
- `_drain_pending_guidance()` 生成 `HumanMessage(additional_kwargs={GUIDANCE_MARKER: True})` 的逻辑不变：`src/voidx/agent/graph/core/voidx_graph.py:415-423`
- `is_guidance_message()` 判断逻辑不变：`src/voidx/llm/message_markers.py:13-14`
- compaction / convergence / token 统计对 guidance message 的处理不变
- `GuidanceSubmitted` 事件继续发出（不删除），`truncated` 字段保留
- `GUIDANCE_MAX_CHARS = 2000` 截断限制不变：`src/voidx/agent/graph/core/voidx_graph.py:74`
- turn 结束时 `_pending_guidance.clear()` 和 `WarningAppended` 逻辑不变：`src/voidx/agent/graph/turn_runner.py:435-443`

### Edge Cases / Failure Paths

| Case | Expected Behavior | Test Coverage |
|------|-------------------|---------------|
| 空文本 guidance | `submit_guidance()` 返回 `False`，不发任何事件 | `src/tests/test_agent/test_guide_command.py`（⚠ 需要新建） |
| 超长 guidance（>2000 字符） | 截断到 2000 字符，`truncated=True`，渲染截断后的文本 | 同上 |
| guidance 发送时 events bus 未运行（`source=\"user\"`） | `emit_direct()` 返回 `False` → 不入队、不发 `GuidanceSubmitted`、`submit_guidance()` 返回 `False`；调用方给用户报错 | 同上 |
| guidance 发送时 events bus 未运行（`source=\"guard\"`） | guard guidance 继续入队，不发 `MessageAppended`（本来就不发），不影响后续 LLM 注入 | `src/tests/test_agent/test_guard_guidance.py` |
| turn 结束时 pending guidance 未被消费 | 发 `WarningAppended("Guidance discarded...")`，清空队列 | `src/tests/test_agent/test_turn_runner.py` |
| guard guidance 调用 `submit_guidance(source="guard")` | 入队但不发 `MessageAppended`，不渲染 | `src/tests/test_agent/test_guard_guidance.py`（⚠ 需要新建） |
| Web 前端 running 时提交 guidance | 不本地追加 guidance 气泡；只依赖后端 `MessageAppended` 渲染，且 Web 端 exactly once | `frontend/test/workbench.test.ts` 或现有前端消息路由测试追加 exactly-once 覆盖 |

### Forbidden Changes

- 不改 `_drain_pending_guidance()` 的 `HumanMessage` 构造逻辑
- 不改 `GUIDANCE_MARKER` 常量值
- 不改 `is_guidance_message()` 判断逻辑
- 不改 compaction / convergence / token 统计中 `is_guidance_message()` 的使用
- 不删除 `GuidanceSubmitted` 事件类型
- 不改 `MessageAppended` 的 schema（不加 `truncated` 字段）
- 不改 `DockEventConsumer` 对 `GuidanceSubmitted` 的 `return None` 行为
- 不让 `GuidanceSubmitted` 继续驱动 Web 可见 message item；实现方式：从 `adapter.py:516` 的 `_ADAPTER_MAP` 中移除 `GuidanceSubmitted: UiEventItemAdapter._on_guidance` 映射行，使该事件在 Web 端变为 no-op。`_on_guidance()` 方法体可保留不删（避免破坏其他引用），但路由表不再指向它。`MessageAppended(style="guidance")` 走独立的 `_on_message()` 路由（`adapter.py:212-215`），不受影响

## Test Plan

| Scenario | Command / Check | Expected Result |
|----------|-----------------|-----------------|
| 用户 guidance 渲染（TUI） | `./test.py --backend -- src/tests/test_agent/test_guide_command.py -v` | （需要新建文件）`submit_guidance(source="user")` 发 `MessageAppended`，`dock.append_message` 被调用 |
| guard guidance 不渲染 | `./test.py --backend -- src/tests/test_agent/test_guard_guidance.py -v` | （需要新建文件）`submit_guidance(source="guard")` 不发 `MessageAppended` |
| 截断 guidance | 单元测试 | `truncated=True`，渲染截断后文本 |
| Web adapter 不渲染 `GuidanceSubmitted` | `./test.py --backend -- src/tests/test_ui/gateway/test_adapter.py -v` | 发 `GuidanceSubmitted` 时 adapter 不产生可见 message item；发 `MessageAppended(style="guidance")` 时产生且仅产生一个 message item |
| bus 未运行时 user guidance 发送失败 | `./test.py --backend -- src/tests/test_agent/test_guide_command.py -v` | `emit_direct` 返回 `False` → `submit_guidance` 返回 `False`，不入队、不发事件 |
| Web 前端 running 时渲染 guidance | `./test.py --frontend -- --reporter=verbose` | running 分支不本地调用 `appendMessageItem`；后端 `MessageAppended` 到达后只渲染一个 `style="guidance"` 气泡 |
| guidance 样式一致性 | 手动检查 | TUI 和 Web guidance 消息与普通用户消息样式一致 |
| 回归：guidance 注入 LLM | `./test.py --backend -- src/tests/test_llm/test_token_counting.py -v` | `GUIDANCE_MARKER` 标记正常，token 统计正确 |

## Open Questions

- [x] 截断提示：在 `submit_guidance()` 内分离 `guidance_text` 与 `display_text`。`guidance_text` 是空白压缩并截断后的纯文本，用于 `_pending_guidance` 和后续 LLM 注入；`display_text` 仅用于 `MessageAppended` UI 渲染，截断时追加 `[truncated]` 标记，避免污染 LLM 输入。
