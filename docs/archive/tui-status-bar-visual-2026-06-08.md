# TUI 状态面板视觉优化

Date: 2026-06-08

> **Status: Done**

Implemented scope: Phase A, Phase B, and Phase C.

## Goal

优化 TUI 底部状态栏的视觉表现，但分阶段落地，避免为了视觉动画引入整帧高频刷新和新的闪烁问题。

本需求拆成三期：

1. **Phase A**：添加语义化颜色，并用方向箭头替代 token `in` / `out` 文案。
2. **Phase B**：添加无定时器的 busy 指示、每轮 activity verb 和当前 turn elapsed。
3. **Phase C**：如果仍需要真正动画，再实现主对话区底部的 turn-level activity line，明确不走整帧刷新。

## Current State

关键文件：

- `src/voidx/ui/tui/renderer.py` — `_status_summary()` 返回纯文本字符串，`_render_hint_lines()` 用统一灰色 `#8F9BA8` 渲染。
- `src/voidx/ui/tui/state.py` — `StatusSummaryCache` 缓存纯文本 `summary: str`。
- `src/voidx/llm/usage.py` — `format_token_count()` / `format_cache_hit_rate()` 格式化 token 数值。

现状问题：

- **全灰无层次**：模型名、策略、状态、token 统计全部同一灰色，无法快速定位信息。
- **in/out 文字冗余**：`in 1.2k out 3.4k` 占用空间且方向感不强。
- **无运行指示**：busy 状态只显示文字 `busy`，没有轻量运行反馈。

## Claude Code 调研摘要

Claude Code 的 spinner 基于 React + Ink，使用 50ms 动画帧、主题色 shimmer、停滞检测和 reduced motion。voidx 是 Python TUI，当前 renderer 依赖手写 ANSI 和 Rich capture，不适合直接复制高频整帧动画。

对 voidx 的适配结论：

| Claude Code 做法 | voidx 适配 |
|-----------------|------------|
| 50ms / 20Hz 动画 | Phase A/B 不做定时动画；Phase C 如需动画，必须只重绘 status/bottom 区域 |
| 橙色 spinner | 可采用 `#D77757` 作为状态/运行色 |
| shimmer 和停滞变红 | 后续再做 |
| 大量随机动词 | 首期不用；默认状态栏保持工具型、可扫读 |
| elapsed 展示 | Phase B 做每轮 activity verb + elapsed |

## Phase A: 颜色与 Token 箭头

### Scope

- 保持 `_status_summary(width) -> str` 的外部行为。
- 不把 `_status_summary()` 改成 `Text | str` 混合返回。
- 新增内部结构化 status segment 构建逻辑，让 `_render_hint_lines()` 能渲染 Rich `Text`。
- 将 token `in` / `out` 文案替换为 `↑` / `↓`。

### Semantic Colors

| Segment | Color | Value | Example |
|---------|-------|-------|---------|
| model | blue | `#6CB6FF` | `openai/gpt-4o` |
| policy | green | `#57AB5A` | `accept-edits workspace-write` |
| state | orange | `#D77757` | `step 3 auto` |
| usage | cyan | `#56D4DD` | `ctx 12k/128k ↑1.2k ↓3.4k` |
| goal | purple | `#C698F0` | `goal active/implement turns 5` |
| separator | dim gray | `#4B5563` | `|` |

### Recommended Implementation

Introduce an internal segment type:

```python
@dataclass(frozen=True)
class StatusSegment:
    kind: str  # model | policy | state | usage | goal
    text: str
```

Recommended flow:

1. `_status_segments()` builds model/policy/state/usage/goal segments.
2. `_select_status_variant(width, segments)` applies the existing width degradation strategy.
3. `_status_summary(width)` keeps returning the selected plain string.
4. `_status_summary_text(width)` or `_render_status_segments(...)` returns Rich `Text` for `_render_hint_lines()`.
5. `StatusSummaryCache.summary` remains `str`; if needed, add cached segment metadata instead of changing summary to `Text | str`.

Token text changes from:

```text
ctx 12k/128k cache 45% in 1.2k out 3.4k total 4.6k
```

to:

```text
ctx 12k/128k cache 45% ↑1.2k ↓3.4k total 4.6k
```

Plain summary and Rich status text must use the same `usage_text`.

## Phase B: 无定时器 Turn Activity Line

### Scope

- No 80ms / 12Hz timer.
- No high-frequency `_render_frame()` calls.
- Turn activity line updates only when an existing render already happens.
- Pick one neutral activity verb per busy turn and keep it stable for that turn.

### Turn Activity Line

When `self._busy` is true, render a single activity line at the bottom of the main conversation area, above the input box:

```text
◐ Contemplating (3s)
```

This line represents the whole turn being in progress. The verb is randomly selected once per turn from a small neutral list (`Working`, `Thinking`, `Reviewing`, `Contemplating`, etc.) and must not change on every tick. The temporary transcript parent used to hold tool calls must not also be called `Working`; use a neutral agent/container label such as `voidx` there. The status bar may keep the static `busy` state word, but it must not carry the dynamic elapsed text.

Phase B may use a fixed centered glyph. If the implementation opportunistically changes the elapsed value based on `time.monotonic()`, it must not imply continuous animation because there is no timer.

### Elapsed Time

In `_consume()`, record the start time when busy begins:

```python
self._busy_started_at = time.monotonic()
```

Clear it when busy ends:

```python
self._busy_started_at = None
```

Elapsed format:

- `< 60s`: `(3s)`
- `>= 60s`: `(1m 23s)`

### Width Budget

The activity line must be clipped to one terminal row and included in the fixed-area row budget.

Recommended flow:

1. Choose and store one activity verb when busy starts.
2. Build activity line text, e.g. `◐ Reviewing (3s)`.
3. Clip it with display-cell width so it never wraps.
4. Reserve one fixed row above the input box.
5. Reduce pinned TODO row budget by this activity row when both are visible.

## Phase C: Timed Turn Activity Line

Phase C adds a real timer, but only for the turn-level activity line.

Requirements:

- The timer must not call `_render_frame()`.
- The timer must repaint only the activity line with fixed row positioning.
- Repaint must not use `ESC[J`; use `ESC[K` to clear the current row tail.
- The transcript temporary agent/container must not use `Working` as its header.
- Busy end and TUI exit must cancel the timer/task.
- Suggested interval: 200-250ms for glyph animation. Elapsed text still derives from monotonic time and only changes once per second.

Possible Phase C features:

- Faster spinner animation beyond the current 250ms centered glyph rotation.
- Stalled detection after 3 seconds without token progress.
- `reducedMotion` fallback to a static glyph.
- User-configurable small verb list.

## Implementation Plan

### Phase A

1. Add status segment helpers in `renderer.py`.
2. Keep `_status_summary(width) -> str`.
3. Add Rich `Text` rendering for status segments in `_render_hint_lines()`.
4. Change usage text to `↑` / `↓`.
5. Update existing status summary tests and add style tests.

### Phase B

1. Add `busy_started_at: float | None` to `RenderState`.
2. Map `_busy_started_at` in `STATE_FIELD_MAP`.
3. Set/clear `_busy_started_at` in `_consume()`.
4. Choose one neutral activity verb per turn.
5. Render `◐ Verb (Ns)` as a width-aware turn activity line.
6. Do not add a timer.

### Phase C

1. Render busy elapsed as a single main-transcript activity line above the input box, not inside the status bar.
2. Reserve the activity line in the fixed-area row budget so it does not collide with pinned TODO.
3. Add a 250ms timer that repaints only the activity line with fixed row positioning and `ESC[K`.
4. Ensure the transcript temporary agent/container uses `voidx`, not `Working`.
5. Ensure timer/task cleanup when busy ends or the TUI exits.

## Affected Files

| Phase | File | Change |
|-------|------|--------|
| A | `src/voidx/ui/tui/renderer.py` | status segments, Rich status text, token arrows |
| A | `src/voidx/ui/tui/state.py` | optionally extend cache metadata; keep `summary: str` |
| A | `tests/test_pure_tui.py` | plain text, color span, narrow width tests |
| B | `src/voidx/ui/tui/state.py` | `busy_started_at` and busy activity verb state |
| B | `src/voidx/ui/tui/app.py` | set/clear busy start time and per-turn verb |
| B | `src/voidx/ui/tui/renderer.py` | turn activity line and elapsed formatting |
| B | `tests/test_pure_tui.py` | activity line, elapsed, status separation tests |
| C | `src/voidx/ui/tui/app.py` / `src/voidx/ui/output/dock/*` | timed activity-line update and non-Working temporary agent label |

## Test Plan

### Phase A Tests

| Test | Coverage |
|------|----------|
| `test_status_summary_plain_text_uses_token_arrows` | Plain summary uses `↑/↓`, not `in ... out ...` |
| `test_status_summary_text_applies_semantic_styles` | model/policy/state/usage/goal and separator styles |
| `test_status_summary_degrades_to_fit_width` | Existing narrow width behavior |
| `test_status_summary_degrades_by_display_width_for_cjk` | CJK width behavior |
| `test_status_summary_reuses_cache_until_marked_dirty` | Cache behavior unchanged |

### Phase B Tests

| Test | Coverage |
|------|----------|
| `test_busy_activity_line_renders_below_temporary_agent_not_status` | Busy elapsed renders as the bottom activity line; temporary agent is not named Working |
| `test_agent_placeholder_keeps_stream_reusable` | Stream replacement still reuses the neutral agent placeholder |
| `test_busy_activity_verb_randomized_once_per_turn` | Activity verb is chosen once per busy turn and cleared when the turn ends |
| `test_busy_started_at_set_and_cleared_by_consume_loop` | `_consume()` records and clears start time |

### Phase C Tests

| Test | Coverage |
|------|----------|
| `test_busy_activity_tick_repaints_bottom_line_with_pinned_todo` | Timer repaints only the bottom activity line and leaves pinned TODO untouched |
| `test_busy_activity_timer_starts_ticks_and_stops` | Busy start creates the timer and busy end cancels it |

## Risks

- **Duplicate Working rows**: only the turn-level bottom activity line may use `Working`; temporary transcript containers use `voidx`.
- **Cache semantics**: dynamic elapsed/spinner data must not be stored as `StatusSummaryCache.summary`.
- **Render flicker**: Phase C must not implement animation by full-frame redraw.
- **Terminal compatibility**: centered spinner glyphs may fail in old terminals; fallback can be `*` or `●`.
- **Visual noise**: verb list stays small and neutral; the chosen verb is stable for the turn.

## Future Work

- Shimmer effect.
- Stalled detection and red spinner.
- reducedMotion support.
- User-configurable verb list.
