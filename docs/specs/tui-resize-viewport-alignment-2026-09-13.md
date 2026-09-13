# TUI 终端尺寸调整动态视口物理对齐与防残影技术规格

- **日期**：2026-09-13
- **状态**：通过独审修订，待实现
- **适用范围**：`tui/voidx_cli/`（`render_frame.py`、`layout.py`、`terminal_writer.py`、`app.py`、`terminal_mixin.py`）
- **受众**：Human + LLM
- **关联规格**：
  - [TUI 视口满载提交与动态区伸缩空行累加修复规范](tui-viewport-full-commit-scrollback-gap-2026-09-12.md)
  - [TUI 动态区悬空与提交空行累加修复技术规格](../archive/tui-anchor-bottom-scrollback-gap-2026-09-11.md)
  - [TUI 提交瞬态行残留清除与帧对齐技术规格](../archive/tui-commit-transient-cleanup-2026-09-11.md)

---

## 1. 问题背景与现场证据

### 1.1 现场截图现象与组件分析

在终端主屏幕运行 TUI 会话期间，用户使用鼠标拖拽终端分栏边框拉小（降低窗口高度）后，终端出现严重的**组件重复残留与空行层叠**现象：

```text
● Prism(对 TUI 启动 Banner 动态保留及提交改造进行独立代码审核)

● Prism(对 TUI 启动 Banner 动态保留及提交改造进行独立代码审核)

● Prism(对 TUI 启动 Banner 动态保留及提交改造进行独立代码审核)
  └─ ● Searching _should_commit_startup

● Wait("Prism")
◓ Tinkering (3m 20s ↑3.7m ↓14.2k)
────────────────────────────────────────────────────────────────────────
› █
────────────────────────────────────────────────────────────────────────
gemini-3.8-flash-tiered max | AI approval | 启动子代理对 TUI 启...
```

#### 现场特征还原
1. **数据模型单节点 vs 屏幕多重残影**：
   - 检查底层数据模型：`OutputTree` 逻辑树中，Subagent Prism 始终只有一个唯一的节点实例，没有在树中重复创建节点。
   - 屏幕上却自上而下并存了 3 个不同时间切片的 `● Prism`：
     - **第 1 行**：子代理刚拉起时的第一帧（尚无子工具）。
     - **第 3 行**：拖动过程中的某一中间帧。
     - **第 5~11 行**：最新的完整有效帧（包含 Prism 及其正在执行的 `Searching` 工具、Wait 节点、Tinkering 动画指示器以及底栏输入框）。
2. **残影永久化**：
   - 上方的旧 Prism 及伴随的空行不是临时刷新闪烁，而是被**固定留在了终端屏幕以及 Scrollback（回滚历史）中**，后续单帧重绘无法自动抹除。

---

## 2. 根本原因剖析 (Root Cause Analysis)

### 2.1 机制一：终端内联模式与底层硬件推滚（Scrollback Push）

voidx TUI 运行在终端的**主屏幕（Main Screen）**模式下，以便保留控制台历史供用户翻阅。
主屏幕具有以下物理约束：
1. **ANSI 序列边界**：光标定位（`CUP \x1b[H`）与屏幕擦除（`ED \x1b[J`）**仅能操作当前可见的终端物理网格（Visible Screen Grid）**。
2. **下压硬件推滚**：当用户向下拉小终端（终端高度 $H$ 缩小，例如从 25 行缩到 11 行）时，如果当前正在展示的内容行数超过了新的物理行数 $H$，终端模拟器（xterm.js / 系统终端 PTY）的底层行为是**将顶部超出部分强制推入终端回滚缓冲区（Scrollback Buffer）**。
3. **推入即失控**：**任何已经推入 Scrollback 的物理行，ANSI 擦除指令均无法再触及或抹去**。后续一旦重绘，新帧只能在下方绘制，导致推入历史的旧行永久裸露。

### 2.2 机制二：擦除起点盲区（`\x1b[{start_row};1H\x1b[J`）

在 `terminal_writer.py` 的 `_write_frame_full` 中：
```python
self._worker_write(f"\x1b[{start_row};1H")
self._worker_write("\x1b[J")
self._worker_write("\n".join(lines))
```
- `\x1b[J`（Erase from cursor to end of screen）**只清除从光标所在行（`start_row`）到屏幕最底部的区域**。
- 窗口拖拽引发尺寸抖动时，上一帧的部分内容留在物理第 $1 \dots (\text{start\_row} - 1)$ 行。
- 当新一帧以计算出的 `start_row > 1`（例如 `start_row = 5`）落笔时，**物理第 1 到 4 行完全未被擦除**，导致先前的残影依然暴露在屏幕顶部。

### 2.3 机制三：纯动态内容缺乏物理容量封顶与 Tail 节点折叠

在视口全是动态未提交内容时（`visible_committed_rows == 0`）：
1. 逻辑动态树若超出当前可用物理空间，系统未对全动态区做针对性的 **容量硬截断（Capacity Clamping）**，导致试图输出超出屏幕高度的行数，触发终端被迫硬件推滚。
2. Resize 发生时，`render_frame.py` 发出的 `kind="resize"` terminal barrier 未能使后续首帧强制重设擦除基准，导致擦除起点未能覆盖到遗留的物理高位。

---

## 3. 设计不变量与架构约束 (Invariants)

1. **坚守连续顶对齐与容量上限原则（Top Alignment with Capacity Ceiling）**：
   - 继承并完全兼容 2026-09-11 规格（`tui-anchor-bottom-scrollback-gap-2026-09-11.md`），**严禁在内容不足时强制将底栏推到底部产生悬空断层**。
   - 内容未满视口时，内容与底栏紧凑连续顶对齐，避免在中间产生物理空白行。
   - 可用容量 $C = \max(1, H - \text{bottom\_rows})$ 作为**动态内容的严格高度上限（Ceiling）**，而非强制填充目标。
2. **纯动态视口零硬件推滚不变量**：
   当视口内不存在已提交历史时（`visible_committed_rows == 0`），**严禁触发任何终端硬件滚屏（DECSTBM 滚屏或换行溢出）**。所有动态内容必须严格适配在物理容量 $C$ 之内，从第 1 行向下绘制。
3. **物理容量硬截断不变量**：
   全动态区向终端输出的渲染总行数 $R_{\text{dynamic}}$ 必须严格满足：
   $$R_{\text{dynamic}} \le H - \text{bottom\_rows}$$
   如果动态逻辑节点总高度超过该值，必须采用结构化折叠/裁剪（优先保留当前活动的叶子工具和输入），绝不溢出造成终端底层推滚。
4. **Resize 屏障原子级全量重绘不变量**：
   在检测到窗口尺寸变化（`resize_frame`）时：
   - 必须提交带有 `invalidate_frame=True` 的 resize barrier；
   - 在纯动态视口下（`visible_committed_rows == 0`），新帧的 `start_row` 必须强制锁定为物理第 1 行；
   - 擦除与新帧写入必须在同一输出批次中原子完成，杜绝清屏与绘制分离导致的高频闪烁。

---

## 4. 核心方案设计 (Design Specification)

### 4.1 视口物理布局与动态底栏模型

终端物理空间自上而下连续排列，底栏高度 $\text{bottom\_rows}$ 为动态测量值（通常 3 行，展开 choice/提示时为 4~10 行）：
```text
┌─────────────────────────────────────────────────────────┐  物理第 1 行
│  上部内容区                                              │
│  - 状态 A（存在已提交历史）：历史区 (visible_committed_rows) │
│               + 动态区（DECSTBM 滚屏安全保护）          │
│  - 状态 B（纯动态区，无历史）：锁定物理第 1 行起连续绘制，    │
│               超高进行节点折叠/Tail截断，严禁滚屏       │
│  最大允许高度: C = max(1, H - bottom_rows)               │
├─────────────────────────────────────────────────────────┤  连续排列
│  底栏区 (Bottom Dock)                                   │
│  - 上分割线、输入框 › █、下分割线/状态条 (或 choice 面板)  │
│  动态高度: bottom_rows 行                                │
└─────────────────────────────────────────────────────────┘
```

#### 极小视口防御（$H \le \text{bottom\_rows}$）
当用户将终端窗口高度拖拽至极小（如 $H \le 4$）：
1. 底栏自动进入超紧凑降级模式：隐藏上下分割线与状态栏，仅保留单行输入框 `› █`（$\text{bottom\_rows} = 1$）；
2. 若 $H \le 1$，输入框高度锁定为 1 行，内容区容量降为 0；
3. 严格保证 $C \ge 0$ 且所有计算坐标 $\ge 1$，彻底消除坐标非正数引发的 `ValueError` 崩溃风险。

### 4.2 场景分支与行为矩阵

| 场景 | 视口历史状态 | 视口超高处理策略 | 擦除与起始坐标 | 硬件滚屏行为 |
| :--- | :--- | :--- | :--- | :--- |
| **A. 包含已提交历史** | `visible_committed_rows > 0` | 允许超高 | `start_row = visible_rows + 1`，顶对齐紧跟历史 | 允许使用 `\x1b[1;{H-bottom_rows}r` 仅将已提交历史推入 Scrollback |
| **B. 纯动态区域** | `visible_committed_rows == 0` | **强制容量上限截断**（最大 $C$ 行） | **锁定物理第 1 行**（`start_row = 1`），全屏擦除 `\x1b[1;1H\x1b[J` | **严禁任何硬件滚屏**，完全在物理视口内重绘 |

### 4.3 纯动态区的结构化折叠与裁剪 (Structural Dynamic Folding)

在 `layout.py` 的 `project_physical_viewport` 中：
1. 计算可用内容容量 $C = \max(0, H - \text{bottom\_rows})$。
2. 按照以下优先级自底向上分配容量：
   - **P1（最高）**：当前活跃的 Thinking 流和 Vibe 提示行；
   - **P2**：Todo 任务面板；
   - **P3**：未提交的活动 Transcript。
3. **结构化节点折叠**：
   - 若 Transcript 无法完整展现，以**节点（Node）**为粒度将较早的已完成子节点折叠为单行 `  … ({hidden_count} steps hidden)`；
   - 保证父子连接引导线（`├─`, `└─`）和 ANSI 颜色闭合完整，不出现悬空断裂。

### 4.4 终端 Resize 响应与防抖防闪烁

1. **尺寸感知与屏障**：
   - 终端发生 Resize 时，`_render_frame()` 检测到 `self._prev_frame_width != width or self._prev_frame_term_height != term_height`。
   - 提交 `kind="resize"` 的 terminal barrier，设置 `invalidate_frame = True`。
2. **原子化擦除输出**：
   - 在 `_write_frame_full` 中：
     - 若 `start_row == 1`（纯动态区），发出 `\x1b[1;1H\x1b[J` 并在同一次 `_worker_write` 中连续追加全部最新行；
     - 避免先发清屏、后等绘制的时间差，彻底消除鼠标拖动时的全屏闪烁。

---

## 5. 改动范围与文件清单

1. **`tui/voidx_cli/layout.py`**：
   - 在 `project_physical_viewport` 中，增加针对 $H \le \text{bottom\_rows}$ 的极小视口防御保护；
   - 确立纯动态视口下严格锁定 `frame_start_row = 1`，并将内容区实际输出行数限制在 $C$ 以内。
2. **`tui/voidx_cli/render_frame.py`**：
   - 在 `_render_frame()` 中，当 `resize_frame` 且 `visible_committed_rows == 0` 时，确保 `force_full = True` 且 `start_row = 1`；
   - 确保 `_submit_terminal_barrier(kind="resize", invalidate_frame=True)` 正确传递刷新标志。
3. **`tui/voidx_cli/terminal_writer.py`**：
   - 确保全量帧在 `start_row == 1` 时具备原子全屏擦除能力（`\x1b[1;1H\x1b[J`），彻底抹平悬空旧行。
4. **`tui/tests/test_frame_advanced.py` / `test_layout_terminal_model.py`**：
   - 增加纯动态未提交场景下的终端高度收缩和扩展回归测试。

---

## 6. 验收标准与测试矩阵 (Verification)

### 6.1 虚拟终端物理模型验证（`_VTScreen`）

1. **`test_resize_pure_dynamic_no_residue_and_no_scrollback`**：
   - 启动虚拟终端模型（高度 24），渲染包含子代理与嵌套工具的多行动态树（总高 15 行）；
   - 动态将高度连续缩小至 11 行，触发 resize 流程；
   - **物理断言**：
     - `screen.scrollback_rows == 0`（完全没有未提交行被挤入 Scrollback）；
     - 检查物理屏幕第 1 行到第 11 行的网格，`● Prism` 出现次数严格为 1，且没有多余残留空行；
     - 底栏严格停靠在物理最底部（第 10~11 行）。
2. **`test_resize_extreme_small_viewport_no_crash`**：
   - 将终端高度分别缩小至 4 行、3 行、2 行；
   - **断言**：无 `ValueError` 异常抛出，底栏自动折叠降级，界面优雅渲染。
3. **`test_resize_top_alignment_when_short`**：
   - 在终端高度 24 行时渲染仅 3 行的短动态组件；
   - **断言**：组件从第 1 行紧凑绘制至第 3 行，底栏紧随其后（第 4~6 行），屏幕中下部自然留白，绝不产生空行悬空与跳行。

### 6.2 禁忌规则 (Forbidden Changes)
- 禁止破坏连续顶对齐模型重新引入 `anchor_bottom` 中间悬空断层。
- 禁止在纯动态未提交状态下发出任何硬件滚屏序列（`\n` 或 `\x1b[1;{B}r`）。
- 禁止破坏 `OutputTree` 的单实例节点模型。
