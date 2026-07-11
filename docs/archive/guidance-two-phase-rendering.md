# Guidance 两阶段渲染 — Implementation Spec

> **Status: Done** — Archived on 2026-07-11.

## Objective

把 `/guide` 消息的渲染从"提交时立即输出到终端"改为两阶段：提交时在 vibe 动态行括号内截断预览，下次 LLM 调用前才正式输出到终端，样式与普通用户消息一致，仅开头标志不同（`⚡` vs `❯`）。

**关键设计决策**：
- guard guidance 与 user guidance 走相同的渲染路径（都发 `GuidanceSubmitted`，都在 vibe 行预览，都在注入时输出到终端）。
- guard guidance 的 emit 失败语义与 user 不同：user guidance emit 失败时返回 `False` 不入队；guard guidance emit 失败时仍入队返回 `True`（保持当前行为，guard guidance 是系统内部信号，不应因 UI 状态丢失）。
- `[truncated]` 标记：`_pending_guidance` 改为存储 `(text, truncated)` 元组而非纯 `str`，drain 时还原 truncated 标记，注入时 `MessageAppended` 的 text 包含 `[truncated]` 后缀（与当前 Phase 1 的 `display_text` 一致）。

## Source of Truth

| Source | Path / Link | Notes |
|--------|-------------|-------|
| Design | 本文档 | 用户已批准方案 |
| Existing Code — submit_guidance | `src/voidx/agent/graph/core/voidx_graph.py:409-433` | source="user" 且 via_events() 时立即发 MessageAppended；guard guidance 不发 MessageAppended 但发 GuidanceSubmitted；emit 失败时 user 返回 False 不入队，guard 仍入队返回 True |
| Existing Code — DockEventConsumer | `src/voidx/ui/output/events/consumers.py:129-189` | MessageAppended → dock.append_message; GuidanceSubmitted → None |
| Existing Code — vibe 行 | `tui/voidx_cli/render_activity.py:72-115` | `_busy_activity_label()` 拼接 details |
| Existing Code — start_turn | `src/voidx/ui/output/dock/app.py:225-244` | 普通用户消息 turn 节点创建 |
| Existing Code — drain | `src/voidx/agent/graph/core/llm.py:211` | `_drain_pending_guidance()` 调用点 |
| Existing Code — tree render | `src/voidx/ui/output/tree.py:482-520,644-668` | depth-1 节点渲染、全宽背景 |
| Existing Code — gateway adapter | `src/voidx/ui/gateway/adapter.py:86-98,213-216,505` | GuidanceSubmitted unmapped; MessageAppended → item.started |

## Current Behavior

- 用户在 agent busy 时输入 `/guide <text>`，TUI 的 `_submit_guidance_bypass()` 发送 `{"kind":"guide", "text": ...}` 到后端。
- 后端 `submit_guidance()` 立即 `emit_direct(MessageAppended(text=display_text, style="guidance"))`，在输出树中创建 `node_type="message"` 节点，header 为 `[guidance]text[/]`。
- `"guidance"` 不是注册的 Rich style，文本无特殊样式，显示为普通文本行。
- guidance 文本同时存入 `_pending_guidance` 队列，在下次 LLM 调用前由 `_drain_pending_guidance()` drain 注入 messages。
- `GuidanceSubmitted` 事件被 `DockEventConsumer` 忽略（返回 `None`），web gateway 也不映射它。
- guard guidance（`source="guard"`）不发 `MessageAppended`，但发 `GuidanceSubmitted`（当 `via_events()` 为 True）。emit 失败时仍入队返回 `True`（与 user guidance 不同，user emit 失败返回 `False` 不入队）。

## Target Behavior

- **提交时**：`submit_guidance()` 不再发 `MessageAppended`，只发 `GuidanceSubmitted`。`DockEventConsumer` 收到 `GuidanceSubmitted` 后调用 `dock.set_guidance_preview(text)`，存到 dock 状态字段，不创建输出树节点。TUI vibe 动态行从 dock 查询 preview，截断后显示在括号内，格式 `⚡{truncated_text}`。多条 guidance 时只显示最后一条（覆盖）。
- **注入时**：`_call_llm()` 中 `_drain_pending_guidance()` 返回后，对每条 guidance 发 `MessageAppended(text=display_text, style="guidance")`（`display_text` 含 `[truncated]` 后缀当 truncated=True），最后发一条 `GuidanceCommitted()`。`DockEventConsumer` 收到 `MessageAppended(style="guidance")` 后调用 `dock.append_guidance_turn(text)`，创建 `node_type="turn"` 节点，header 为 `[bold white]⚡[/] {text}`，全宽背景、空行分隔、body 缩进，与普通用户消息一致。`GuidanceCommitted` 调用 `dock.clear_guidance_preview()` 清除 vibe 行 preview（只在所有 MessageAppended 发完后清除，避免多条 guidance 时过早清除）。
- guard guidance 也走同样路径（发 `GuidanceSubmitted`，在 vibe 行显示，注入时输出到终端）。但 emit 失败语义不同：guard emit 失败时仍入队返回 `True`。
- web gateway 适配：`GuidanceSubmitted` 映射为 preview 通知；`MessageAppended(style="guidance")` 继续映射为 `item.started`（注入时渲染）；`GuidanceCommitted` 映射为清除 preview 的通知。

## Files to Change

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `src/voidx/agent/graph/core/voidx_graph.py` | modify | `submit_guidance()`: 删除 `MessageAppended` 发送，只发 `GuidanceSubmitted`；guard guidance 也发 `GuidanceSubmitted`；`_pending_guidance` 改为存储 `(text, truncated)` 元组；`_drain_pending_guidance()` 返回 `list[tuple[HumanMessage, bool]]` | `_pending_guidance` 的 FIFO 队列语义、`GUIDANCE_MAX_CHARS` 截断逻辑、`HumanMessage` 的 `GUIDANCE_MARKER` |
| `src/voidx/agent/graph/contracts.py` | modify | `_pending_guidance` 类型从 `list[str]` 改为 `list[tuple[str, bool]]` | 其他字段 |
| `src/voidx/agent/graph/tool_executor/guards.py` | modify | `_submit_guard_guidance()` fallback 路径（line 149-151）：`pending.append(guidance.message)` 改为 `pending.append((guidance.message, False))` | `submit_guidance` 调用路径 |
| `src/voidx/agent/graph/core/llm.py` | modify | `_call_llm()`: drain 后对每条 guidance 发 `MessageAppended(style="guidance")` + `GuidanceCommitted`；需新增 `from voidx.ui.output.events import MessageAppended, GuidanceCommitted` 导入 | drain 返回值、messages 注入逻辑 |
| `src/voidx/ui/output/events/schema.py` | modify | 新增 `GuidanceCommitted` 事件类型 | 现有事件类型 |
| `src/voidx/ui/output/events/__init__.py` | modify | 导出 `GuidanceCommitted` | |
| `src/voidx/ui/output/events/consumers.py` | modify | `GuidanceSubmitted` → `dock.set_guidance_preview(text)`；`MessageAppended(style="guidance")` → `dock.append_guidance_turn(text)`；`GuidanceCommitted` → `dock.clear_guidance_preview()` | 其他事件处理 |
| `src/voidx/ui/output/dock/app.py` | modify | 新增 `_guidance_preview: str` 字段；`set_guidance_preview()` / `clear_guidance_preview()` / `append_guidance_turn()` 方法 | `start_turn()` 逻辑 |
| `src/voidx/ui/output/dock/status.py` | modify | 新增 `active_guidance_preview_text()` 模块级函数 | 现有 status 函数 |
| `src/voidx/ui/output/dock/__init__.py` | modify | 导出 `active_guidance_preview_text` | |
| `src/voidx/runtime/ui.py` | modify | 新增 `GuidanceCommitted = _LazyAttr("voidx.ui.output.events", "GuidanceCommitted")` | 现有 `_LazyAttr` 条目 |
| `tui/voidx_cli/render_activity.py` | modify | `_busy_activity_label()` details 中加入 `⚡{truncated_preview}` | 现有 details 拼接逻辑 |
| `src/voidx/ui/gateway/adapter.py` | modify | `GuidanceSubmitted` 映射为 preview 通知；`GuidanceCommitted` 映射为清除 preview | 现有 handler 逻辑 |
| `src/tests/test_agent/test_guide_command.py` | modify | 更新断言：不再期望 `MessageAppended`，改为只期望 `GuidanceSubmitted` | |
| `src/tests/test_ui/gateway/test_ui_events_dock_prompts.py` | modify | 更新 guidance dock 测试 | |
| `src/tests/test_ui/gateway/test_adapter.py` | modify | 更新 guidance adapter 测试 | |
| `src/tests/test_agent/test_guard_guidance.py` | modify | 更新断言：guard guidance 现在也发 `GuidanceSubmitted`（当前已发）；emit 失败时仍入队返回 `True` 的行为保持；`_pending_guidance` 改为元组后需适配 | |

## Invariants

- `_pending_guidance` 队列 FIFO 语义不变：`submit_guidance()` 追加，`_drain_pending_guidance()` 弹出。存储格式从 `list[str]` 改为 `list[tuple[str, bool]]`（text, truncated），但 FIFO 顺序和 clear 逻辑不变。
- `submit_guidance(source="user")` emit 失败时返回 `False` 不入队（保持当前行为）。
- `submit_guidance(source="guard")` emit 失败时仍入队返回 `True`（保持当前行为，guard guidance 是系统内部信号）。
- guidance `HumanMessage` 的 `GUIDANCE_MARKER` additional_kwargs 不变。
- 普通用户消息（`start_turn`）的渲染逻辑不变。
- `safe_flush_line_count` 的 settled 链检查逻辑不变。
- vibe 动态行在非 busy 状态下不渲染（`_render_busy_activity_elements` 的 `if not self._busy: return []` 不变）。
- turn 之间的空行逻辑（`_needs_gap_between_root_blocks`）不变。

## Implementation Requirements

### Functional Requirements

- [ ] `submit_guidance(source="user")` 不再发 `MessageAppended`，只发 `GuidanceSubmitted(text, truncated)`。emit 失败时返回 `False`（不入队）。
- [ ] `submit_guidance(source="guard")` 也发 `GuidanceSubmitted(text, truncated)`（truncated 由截断逻辑计算，与 user 一致）。emit 失败时仍入队返回 `True`（与 user 不同）。
- [ ] `_pending_guidance` 改为存储 `(text, truncated)` 元组。`_drain_pending_guidance()` 改为返回 `list[tuple[HumanMessage, bool]]`（message, truncated）。`_call_llm()` 中 drain 后需解包：`guidance_messages = [msg for msg, _ in drained]` 用于注入 LLM，`truncated_flags = [t for _, t in drained]` 用于构造 `display_text`。注意 `rebuild_llm_messages` 中 `[*messages, *guidance_messages]` 的使用方式需适配（guidance_messages 仍为 `list[HumanMessage]`）。
- [ ] `DockEventConsumer.handle(GuidanceSubmitted)` 调用 `dock.set_guidance_preview(text)`。
- [ ] `DockEventConsumer.handle(MessageAppended(style="guidance"))` 调用 `dock.append_guidance_turn(text)` 而非 `dock.append_message(text, style="guidance")`。
- [ ] `DockEventConsumer.handle(GuidanceCommitted)` 调用 `dock.clear_guidance_preview()`。
- [ ] `dock.set_guidance_preview(text)` 存储截断后的 preview 文本到 `self._guidance_preview`，调用 `self.refresh()`。
- [ ] `dock.clear_guidance_preview()` 清空 `self._guidance_preview`，调用 `self.refresh()`。
- [ ] `dock.append_guidance_turn(text)` 创建 `node_type="turn"` 节点，header 为 `[bold white]⚡[/] {escaped_text}`，复用 `_render_turn_text()`，`_mark_settled()`，调用 `self.refresh()`。不在此处清除 guidance preview（preview 由 `GuidanceCommitted` 事件统一清除）。节点插入位置：如果 `_stream_node` 存在则插入到其之前（`before_active_stream=True`），否则追加到 root。
- [ ] `active_guidance_preview_text()` 返回 `dock._guidance_preview`（空字符串时返回 `""`）。
- [ ] `_busy_activity_label()` 在 details 末尾加入 `⚡{preview}`，preview 用 `_clip_cells` 截断到 40 字符，超出加 `…`。
- [ ] `_call_llm()` 在 drain 后，对每条 guidance 发 `MessageAppended(text=display_text, style="guidance")`，其中 `display_text = f"{text} [truncated]" if truncated else text`（truncated 信息从 drain 结果获取），最后发 `GuidanceCommitted()`。仅在 `via_events()` 为 True 时发送。
- [ ] web gateway `GuidanceSubmitted` 映射为 `item.started` kind=`"guidance_preview"`；`GuidanceCommitted` 映射为清除 preview 的通知。

### Error Handling

- [ ] `submit_guidance(source="user")` emit `GuidanceSubmitted` 失败时返回 `False` 不入队（保持当前 user guidance 的 emit 失败语义）。
- [ ] `submit_guidance(source="guard")` emit `GuidanceSubmitted` 失败时仍入队返回 `True`（保持当前 guard guidance 的 emit 失败语义，guard guidance 是系统内部信号，不应因 UI 状态丢失）。
- [ ] `_call_llm()` 中发 `MessageAppended` / `GuidanceCommitted` 时如果 `via_events()` 为 False，跳过（非 events 模式下 guidance 不渲染到终端，与当前行为一致——当前非 events 模式下 guidance 只入队不显示）。

### Data / Migration Requirements

- [ ] N/A — 无持久化数据变更，guidance preview 是纯运行时状态。

### API / Compatibility Requirements

- [ ] `GuidanceSubmitted` 事件 schema 不变（已有 `text` 和 `truncated` 字段），新增 `GuidanceCommitted` 事件。
- [ ] `MessageAppended(style="guidance")` 的 schema 不变，但语义从"立即显示普通 message"变为"注入时显示 guidance turn"。
- [ ] web gateway 新增 `guidance_preview` item kind，前端需适配（但前端适配不在本 spec 范围内）。

## Edge Cases

| Case | Required Behavior | Verification |
|------|-------------------|--------------|
| 空 guidance（`/guide` 无参数） | `submit_guidance()` 返回 `False`，不发事件，不入队 | `test_submit_guidance_rejects_blank_text_without_events` |
| 超长 guidance（> GUIDANCE_MAX_CHARS） | 截断后存入队列，`GuidanceSubmitted.truncated=True`，preview 显示截断后的文本 | `test_submit_user_guidance_marks_truncated_display_without_polluting_llm_input` |
| 多条 guidance（同一 turn 内多次 `/guide`） | vibe 行只显示最后一条（`set_guidance_preview` 覆盖）；注入时每条各自输出一个 guidance turn 节点 | 新增测试 |
| emit `GuidanceSubmitted` 失败（user） | 返回 `False`，不入队 | `test_submit_user_guidance_fails_without_queueing_when_visible_emit_fails` |
| emit `GuidanceSubmitted` 失败（guard） | 仍入队返回 `True` | `test_submit_guard_guidance_stays_hidden_and_queues_when_event_bus_rejects` |
| guidance 提交后 turn 结束但无 LLM 调用 | `turn_runner.py` finally 块中 `_pending_guidance` 被 clear，发 `WarningAppended`；preview 状态残留但 turn 结束后 vibe 行不再渲染 | 新增测试（现有 `turn_runner.py:435-445` finally 块逻辑无测试覆盖） |
| guard guidance | 与 user guidance 走同样路径（发 `GuidanceSubmitted`，preview 显示，注入时输出），但 emit 失败时仍入队 | `test_guard_guidance.py` 更新 |
| 非 events 模式（`via_events()` 为 False） | `submit_guidance` 不发事件只入队；`_call_llm` 不发渲染事件；guidance 不显示在终端 | 现有行为保持 |

## Forbidden Changes

- Do not modify `start_turn()` 的 header 格式或 node_type。
- Do not modify `_drain_pending_guidance()` 的 `HumanMessage` 构造（content、additional_kwargs）。返回值类型可从 `list[HumanMessage]` 改为 `list[tuple[HumanMessage, bool]]` 以传递 truncated 信息。
- Do not modify `_needs_gap_between_root_blocks()` 的空行逻辑。
- Do not modify `safe_flush_line_count()` 或 `_is_node_chain_settled()`。
- Do not modify `_busy_activity_label()` 中除 guidance preview 外的 details 拼接。
- Do not change `_pending_guidance` 的队列语义（FIFO、clear 逻辑）。
- Do not add new dependencies.
- Do not modify `submit_guidance()` 的截断逻辑（`GUIDANCE_MAX_CHARS`）。

## Tests

| Test Level | Command | Expected Result |
|------------|---------|-----------------|
| Focused — guide command | `./test.py --backend -- src/tests/test_agent/test_guide_command.py -v` | submit_guidance 只发 GuidanceSubmitted，不发 MessageAppended |
| Focused — dock prompts | `./test.py --backend -- src/tests/test_ui/gateway/test_ui_events_dock_prompts.py -v` | GuidanceSubmitted 设置 preview；MessageAppended(style="guidance") 创建 turn 节点 |
| Focused — gateway adapter | `./test.py --backend -- src/tests/test_ui/gateway/test_adapter.py -v` | GuidanceSubmitted 映射为 preview 通知；GuidanceCommitted 映射为清除 |
| Focused — TUI activity | `./test.py --backend -- tui/tests/test_status_activity.py -v` | vibe 行包含 `⚡{truncated_preview}` |
| Focused — guard guidance | `./test.py --backend -- src/tests/test_agent/test_guard_guidance.py -v` | guard guidance 发 GuidanceSubmitted；emit 失败仍入队 |
| Regression — slash session | `./test.py --backend -- src/tests/test_agent/slash/test_slash_session.py -v` | `/guide` slash 命令行为不变 |
| Regression — run loop startup | `./test.py --backend -- src/tests/test_agent/graph/test_run_loop_startup.py -v` | web command guide 路径不变 |
| Regression — full backend | `./test.py --backend` | 全绿 |
| Regression — full TUI | `./test.py --backend -- tui/tests/` | 全绿 |

## Definition of Done

- [ ] All functional requirements are implemented.
- [ ] Existing invariants still hold.
- [ ] Edge cases above are covered by tests or documented manual checks.
- [ ] Verification commands pass with captured output.
- [ ] No unrelated files were changed.
