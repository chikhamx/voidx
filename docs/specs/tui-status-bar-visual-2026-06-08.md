# TUI 状态面板视觉优化

Date: 2026-06-08

## Goal

优化 TUI 底部状态栏的视觉表现：添加语义化颜色、用方向箭头替代 token in/out 文字、agent 运行时显示动态旋转图标。

## Current State

关键文件：

- `src/voidx/ui/tui/renderer.py` — `_status_summary()` 返回纯文本字符串，`_render_hint_lines()` 用统一灰色 `#8F9BA8` 渲染。
- `src/voidx/ui/tui/state.py` — `StatusSummaryCache` 缓存纯文本 `summary: str`。
- `src/voidx/llm/usage.py` — `format_token_count()` / `format_cache_hit_rate()` 格式化 token 数值。

现状问题：

- **全灰无层次** — 模型名、策略、状态、token 统计全部同一灰色，无法快速定位信息。
- **in/out 文字冗余** — `in 1.2k out 3.4k` 占用空间且方向感不强。
- **无运行指示** — busy 状态只显示文字 "busy"，没有动态视觉反馈，用户难以感知 agent 是否在活跃执行。

## Claude Code 调研

Claude Code 的 spinner 系统基于 React + Ink（终端 React 渲染器），架构如下：

### 整体架构

```
Spinner.tsx (顶层，~562行)
  └─ SpinnerAnimationRow.tsx (50ms 动画帧驱动)
       ├─ SpinnerGlyph.tsx (旋转字形 + 颜色插值)
       ├─ ShimmerChar.tsx (逐字符闪烁高亮)
       ├─ useStalledAnimation.ts (3秒无token变红)
       └─ useShimmerAnimation.ts (shimmer 波浪)
```

### 关键实现细节

**1. 旋转字形 — 非 braille，用装饰符号**

Claude Code **不使用** braille 点阵（`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`），而是使用装饰性 Unicode 符号序列：

```typescript
// utils.ts — getDefaultCharacters()
function getDefaultCharacters(): string[] {
  if (process.env.TERM === 'xterm-ghostty') {
    return ['·', '✢', '✳', '✶', '✻', '']  // Ghostty 特殊处理
  }
  return process.platform === 'darwin'
    ? ['·', '✢', '✳', '✶', '✻', '✽']       // macOS
    : ['·', '✢', '', '✶', '✻', '✽']         // Linux
}
```

帧序列 = 正序 + 逆序，形成往返动画：
```typescript
const SPINNER_FRAMES = [...DEFAULT_CHARACTERS, ...[...DEFAULT_CHARACTERS].reverse()]
// 效果: · ✢ ✳ ✶ ✻ ✽ ✻ ✶ ✳ ✢ · ✢ ✳ ... (来回摆动)
```

**2. 动画帧率 — 50ms (20Hz)**

```typescript
// SpinnerAnimationRow.tsx
const [viewportRef, time] = useAnimationFrame(reducedMotion ? null : 50)
```

50ms 间隔 = 每秒 20 帧，比我们之前设计的 6Hz (167ms) 流畅很多。帧索引由 `time` 驱动：

```typescript
// SpinnerGlyph.tsx
const spinnerChar = SPINNER_FRAMES[frame % SPINNER_FRAMES.length]
```

`frame` 由父组件的动画时钟递增。

**3. 颜色 — Claude 橙色 + Shimmer 效果**

主题色定义（`theme.ts`）：

| 色名 | 暗色主题值 | 用途 |
|------|-----------|------|
| `claude` | `rgb(215,119,87)` | spinner 主色（橙色） |
| `claudeShimmer` | `rgb(235,159,127)` | shimmer 高亮色（浅橙） |
| `claudeBlue_FOR_SYSTEM_SPINNER` | `rgb(147,165,255)` | 系统 spinner 蓝色 |
| `claudeBlueShimmer_FOR_SYSTEM_SPINNER` | `rgb(177,195,255)` | 系统 shimmer 浅蓝 |

Shimmer 效果：逐字符高亮波浪，当前字符和相邻字符用 `shimmerColor`，其余用 `messageColor`：

```typescript
// ShimmerChar.tsx
const isHighlighted = index === glimmerIndex
const isNearHighlight = Math.abs(index - glimmerIndex) === 1
const shouldUseShimmer = isHighlighted || isNearHighlight
const color = shouldUseShimmer ? shimmerColor : messageColor
```

**4. 停滞检测 — 3秒无新 token 变红**

```typescript
// useStalledAnimation.ts
const isStalled = timeSinceLastToken > 3000 && !hasActiveTools
const intensity = isStalled
  ? Math.min((timeSinceLastToken - 3000) / 2000, 1)  // 2秒渐变到全红
  : 0
```

停滞时颜色从主题色插值到红色 `rgb(171,43,63)`：

```typescript
// SpinnerGlyph.tsx
if (stalledIntensity > 0) {
  const interpolated = interpolateColor(baseRGB, ERROR_RED, stalledIntensity)
  return <Text color={toRGBColor(interpolated)}>{spinnerChar}</Text>
}
```

**5. 无障碍 — reducedMotion 模式**

```typescript
// SpinnerGlyph.tsx
if (reducedMotion) {
  const isDim = Math.floor(time / 1000) % 2 === 1  // 2秒周期：1秒亮、1秒暗
  return <Text color={messageColor} dimColor={isDim}>●</Text>
}
```

**6. 动词轮换 + 已用时间**

Spinner 旁显示当前动作动词（如 "Thinking"、"Analyzing"）和已用时间（如 `(3s)`、`(47s)`），动词从预设列表随机选取。

### 对 voidx 的启示

| Claude Code 做法 | voidx 适配建议 |
|-----------------|---------------|
| 装饰符号 `· ✢ ✳ ✶ ✻ ✽` 往返动画 | 保留 braille `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` 单向旋转，更经典、终端兼容性更好 |
| 50ms (20Hz) 帧率 | 采用 80ms (~12Hz)，平衡流畅度与 CPU 开销。voidx 是 Python 单线程，20Hz 渲染开销偏高 |
| 橙色 `rgb(215,119,87)` + shimmer | 主色用 `#D77757`（与 Claude Code 一致的橙色），暂不做 shimmer（复杂度高、收益低） |
| 3秒停滞变红 | 值得后续加入，但首期不做 |
| reducedMotion 支持 | 值得后续加入 |
| 动词 + 已用时间 | 加入动词轮换 + 已用时间显示，参考 Claude Code 的 ~150 个动词列表 |

## Design

### 1. 语义化颜色

将 `_status_summary()` 返回类型从 `str` 改为 `Text`（Rich 的富文本对象），各区域着色：

| 区域 | 颜色 | 色值 | 示例 |
|------|------|------|------|
| 模型/Provider | 蓝色 | `#6CB6FF` | `openai/gpt-4o` |
| 策略（权限/沙箱/审批） | 绿色 | `#57AB5A` | `accept-edits workspace-write` |
| 状态（busy/step/mode） | 橙色 | `#D77757` | `⠋ step 3 auto` |
| Token 统计 | 青色 | `#56D4DD` | `ctx 12k/128k ↑1.2k ↓3.4k` |
| Goal | 紫色 | `#C698F0` | `goal active/implement turns 5` |
| 分隔符 `|` | 暗灰 | `#4B5563` | `|` |

> 状态区域橙色 `#D77757` 与 Claude Code 的 `rgb(215,119,87)` 一致。

实现方式：

```python
# _status_summary() 内部构建 Text 对象
text = Text("  ")
text.append(model_text, style="#6CB6FF")
text.append(" | ", style="#4B5563")
text.append(policy_text, style="#57AB5A")
text.append(" | ", style="#4B5563")
text.append(state_text, style="#D77757")
text.append(" | ", style="#4B5563")
text.append(usage_text, style="#56D4DD")
text.append(" | ", style="#4B5563")
text.append(goal_text, style="#C698F0")
```

需要同步修改：

- `StatusSummaryCache.summary` 类型从 `str` 改为 `Text | str`
- `_render_hint_lines()` 判断返回值类型，`Text` 直接使用，`str` 降级为灰色

### 2. Token 方向箭头

将 `in` / `out` 替换为 `↑` / `↓`：

```
# Before
ctx 12k/128k cache 45% in 1.2k out 3.4k total 4.6k

# After
ctx 12k/128k cache 45% ↑1.2k ↓3.4k total 4.6k
```

改动点：`_status_summary()` 中 `usage_text` 的格式化字符串。

### 3. 动态旋转图标

Agent 运行时（`self._busy` 为 True），用 braille 旋转动画替代静态 "busy" 文字：

```
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
```

帧切换逻辑：

- 基于 `time.monotonic()` 计算当前帧索引：`frame = int(t * 12) % len(_SPINNER_FRAMES)`
- ~12Hz 刷新率（80ms 间隔），平衡流畅度与 Python 渲染开销
- 旋转图标着橙色 `#D77757`，与状态区域同色

```
# Before
busy step 3 auto

# After
⠋ step 3 auto
```

**缓存影响**：当前 `_status_summary` 用 snapshot 做缓存，`self._busy` 是 snapshot 的一部分。旋转图标需要更频繁刷新，但不应破坏缓存机制。

**采用渲染层叠加方案**：不在 `_status_summary` 中处理 spinner，而是在 `_render_hint_lines` 中，当 `self._busy` 时，在状态 Text 前拼接一个带颜色的 spinner Text：

```python
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def _render_hint_lines(self) -> list:
    lines: list = []
    status = self._status_summary(self._frame_width())
    if status:
        if isinstance(status, Text) and self._busy:
            import time
            frame = _SPINNER_FRAMES[int(time.monotonic() * 12) % len(_SPINNER_FRAMES)]
            spinner = Text(frame + " ", style="#D77757")
            status = spinner + status  # Text 支持拼接
        if isinstance(status, Text):
            lines.append(status)
        else:
            lines.append(Text(status, style="#8F9BA8"))
    ...
```

这样 `_status_summary` 的缓存完全不受影响，spinner 仅在渲染层叠加。`_status_summary` 中 busy 仍输出 "busy" 文字，但 spinner 图标在前面，视觉上 "busy" 文字可保留或移除（建议移除，因为 spinner 已表达运行状态）。

### 4. 动词轮换

Agent 运行时，spinner 旁显示一个随机动词，每次新 turn 随机选取一个，增加趣味性。

```
# Before
⠋ busy step 3 auto

# After
⠋ Pondering step 3 auto
```

动词列表参考 Claude Code 的 `spinnerVerbs.ts`（~150 个），精选适合编程场景的有趣动词，分为三类：

**编程/思考类（严肃但有趣）：**

```
Architecting, Bootstrapping, Calculating, Cerebrating, Cogitating,
Composing, Computing, Concocting, Contemplating, Crafting, Creating,
Crunching, Crystallizing, Deciphering, Deliberating, Determining,
Elucidating, Envisioning, Forging, Generating, Hashing, Hatching,
Ideating, Imagining, Incubating, Inferring, Manifesting, Mulling,
Musing, Orchestrating, Pondering, Processing, Propagating, Ruminating,
Sketching, Spinning, Synthesizing, Tinkering, Transmuting, Unfurling,
Unravelling, Working, Wrangling
```

**烹饪/化学类（Claude Code 特色风格）：**

```
Baking, Brewing, Caramelizing, Churning, Cooking, Fermenting,
Flambéing, Garnishing, Infusing, Julienneing, Kneading, Leavening,
Marinating, Percolating, Sautéing, Seasoning, Simmering, Stewing,
Tempering, Whisking, Zesting
```

**荒诞/趣味类（惊喜感）：**

```
Boondoggling, Booping, Canoodling, Combobulating, Discombobulating,
Doodling, Finagling, Flibbertigibbeting, Flummoxing, Gallivanting,
Hullaballooing, Lollygagging, Moonwalking, Noodling, Prestidigitating,
Quantumizing, Razzle-dazzling, Recombobulating, Reticulating,
Shenaniganing, Skedaddling, Tomfoolering, Whatchamacalliting,
Zigzagging
```

实现方式：

```python
# src/voidx/ui/tui/spinner_verbs.py
_SPINNER_VERBS = [
    # 编程/思考类
    "Architecting", "Bootstrapping", "Calculating", "Cerebrating",
    "Cogitating", "Composing", "Computing", "Concocting",
    "Contemplating", "Crafting", "Creating", "Crunching",
    "Crystallizing", "Deciphering", "Deliberating", "Determining",
    "Elucidating", "Envisioning", "Forging", "Generating",
    "Hashing", "Hatching", "Ideating", "Imagining",
    "Incubating", "Inferring", "Manifesting", "Mulling",
    "Musing", "Orchestrating", "Pondering", "Processing",
    "Propagating", "Ruminating", "Sketching", "Spinning",
    "Synthesizing", "Tinkering", "Transmuting", "Unfurling",
    "Unravelling", "Working", "Wrangling",
    # 烹饪/化学类
    "Baking", "Brewing", "Caramelizing", "Churning",
    "Cooking", "Fermenting", "Garnishing", "Infusing",
    "Kneading", "Leavening", "Marinating", "Percolating",
    "Sautéing", "Seasoning", "Simmering", "Stewing",
    "Tempering", "Whisking", "Zesting",
    # 荒诞/趣味类
    "Boondoggling", "Booping", "Canoodling", "Combobulating",
    "Discombobulating", "Doodling", "Finagling",
    "Flibbertigibbeting", "Flummoxing", "Gallivanting",
    "Hullaballooing", "Lollygagging", "Moonwalking",
    "Noodling", "Prestidigitating", "Quantumizing",
    "Razzle-dazzling", "Recombobulating", "Reticulating",
    "Shenaniganing", "Skedaddling", "Tomfoolering",
    "Whatchamacalliting", "Zigzagging",
]

def pick_spinner_verb() -> str:
    import random
    return random.choice(_SPINNER_VERBS)
```

动词选取时机：每次 `_busy` 从 False 变为 True 时（即新 turn 开始），随机选取一个动词，存入 render state，整个 turn 期间保持不变。

```python
# app.py 中 busy 状态切换时
if not was_busy and is_busy:
    self._spinner_verb = pick_spinner_verb()
```

### 5. 已用时间显示

Spinner 旁显示当前 turn 的已用时间，格式 `(Ns)` 或 `(Nm Ns)`：

```
⠋ Pondering (3s) step 3 auto
⠋ Kneading (1m 23s) step 5 auto
```

实现：记录 turn 开始时间 `time.monotonic()`，在 `_render_hint_lines` 中计算差值。时间显示放在动词之后、step 之前。

### 6. Spinner 驱动渲染刷新

当前 TUI 在 busy 时依赖 `_run_scheduled_render` 定时刷新。需要确保刷新间隔 ≤ 80ms 以支撑 12Hz spinner 动画。

检查 `app.py` 中的定时渲染逻辑，确认 `invalidate()` 在 busy 状态下的调用频率。如果当前刷新间隔 > 80ms，需要缩短或增加 busy 专用的定时 invalidate。

## Affected Files

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/tui/renderer.py` | `_status_summary()` 返回 `Text`；`_render_hint_lines()` 适配 + spinner + 动词 + 已用时间；`usage_text` 用 ↑↓ |
| `src/voidx/ui/tui/state.py` | `StatusSummaryCache.summary` 类型改为 `Text \| str`；导入 `Text`；新增 `_spinner_verb` / `_turn_start_time` 字段 |
| `src/voidx/ui/tui/app.py` | busy 切换时选取动词 + 记录 turn 开始时间；确保 busy 时渲染刷新频率 ≥ 12Hz |
| `src/voidx/ui/tui/spinner_verbs.py` | 新增：动词列表 + `pick_spinner_verb()` |

## Risks

- **cell_len 计算变化** — `Text` 对象的 `cell_len` 与纯文本不同，宽度裁剪逻辑（variants 循环）需要用 `text.cell_len` 而非 `rich.cells.cell_len`。需确保 `_clip_cells` 也能处理 `Text` 对象，或在裁剪时先 `.plain` 取纯文本测宽。
- **缓存 key 语义** — `StatusSummaryCache.summary` 从 `str` 变为 `Text | str`，如果有其他代码按 `str` 读取 cache.summary，需要适配。当前仅 renderer 内部使用，风险可控。
- **Spinner 刷新率** — 依赖渲染循环频率，如果渲染不频繁则动画不流畅。需验证 busy 时 invalidate 间隔。
- **终端兼容性** — braille 点阵字符在极少数终端（Windows cmd.exe 旧版）可能渲染异常，但现代终端均支持。

## Future Work（参考 Claude Code，首期不做）

- **Shimmer 效果** — 逐字符高亮波浪，需要更细粒度的 Text span 控制
- **停滞检测** — 3秒无新 token 时 spinner 渐变红色
- **reducedMotion** — 系统无障碍设置检测，降级为静态 `●` 闪烁
- **用户自定义动词** — Claude Code 支持用户通过 settings 追加/替换动词列表
