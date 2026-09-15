# TUI 视口满载提交与动态区伸缩空行累加修复规范

> **Status: Done** — Archived on 2026-09-16.

- **日期**：2026-09-12
- **状态**：设计完成，待实现
- **适用范围**：`tui/voidx_cli/` 终端物理视口渲染与提交管道（`render_frame.py`、`commit_output.py`、`commit_geometry.py`、`app.py`、`terminal_writer.py`）
- **受众**：Human + LLM
- **关联规格**：[TUI 动态区悬空与提交空行累加修复技术规格](../archive/tui-anchor-bottom-scrollback-gap-2026-09-11.md)、[TUI 提交瞬态行残留清除与帧对齐技术规格](../archive/tui-commit-transient-cleanup-2026-09-11.md)

---

## 1. 问题背景与现场证据

### 1.1 用户反馈与典型现象

用户在终端 TUI 运行多轮工具调用（连续 Bash、Read 等）时反馈：
> **“这几段空行是如何产生的……好像在可视窗口被 commit 满了就容易出现”**

#### 现场截图特征还原
截图中展示了连续的工具执行记录，具有以下明显特征：
1. **段内紧密排列**：
   - 第一段末尾：`Bash` 读取会话目录等命令紧凑排列。
   - 第二段（空行后）：连续 9 行 `Bash("./python.py -c ...")` 紧密排列，行间完全无缝。
   - 第三段（空行后）：连续 13 行 `Bash` / `Read`（包括检索 `llm_requests`、`thinking.py`、`streaming.py`）紧密排列。
   - 第四段（空行后）：2 行 `Bash` / `Read` 检索 `_looks_like_provider_tool_call_fragment`。
   - 第五段（空行后）：1 行失败命令 `✗ Bash(...) exit 2`。
2. **段落间存在整齐空行**：
   - 在上述各段工具群组之间，出现整齐的 **1 ~ 3 行空白**。
   - 空行不是临时刷新闪烁，而是**永久留在终端的 Scrollback（回滚缓冲区）历史中**，翻看上滚历史时依然存在。

### 1.2 数据模型与渲染管线证据

经核查会话数据记录（Session `63e0d92cd571`，Record 40 ~ 75）：
1. **消息层与转录层无语义空行**：
   - 检查 `messages.jsonl`，所有 assistant 消息的 `content` 均为 `""`，仅包含 `tool_calls`；对应 tool 消息为执行输出。
   - 检查 `transcript.jsonl`，Record 45 ~ 72 的 `node_type` 全部为 `tool_call`，`parent_node_id` 全为 1，状态均为 `done` 或 `error`，`collapsed=True`。
2. **OutputTree 逻辑渲染无空行**：
   - 执行 `OutputTree.render_with_line_map(80)` 重放该段记录：连续紧凑节点（`tool_call` 与 `tool_call`）满足 `compact_types = {"assistant", "message", "tool_call"}`，`_needs_gap_between_message_blocks` 均返回 `False`。
   - 逻辑渲染输出的所有文本行是连续相邻的，**逻辑树中不存在任何导致这几处空行的空字符串行**。
3. **结论**：
   - **空行并非源自数据模型或排版间隔，而是终端物理视口（Viewport）在满载滚屏（Hardware Scrolling）与动态活动帧（Dynamic Frame）伸缩交替时产生的物理行泄漏。**

---

## 2. 根本原因剖析 (Root Cause Analysis)

### 2.1 机制一：视口满载与 DECSTBM 硬件受保护滚屏

TUI 采用主屏幕内联（Inline）渲染模型，屏幕划分为：
- **已提交历史区（Committed History）**：已完成并提交到终端 scrollback 的行，可见行数为 $V$。
- **保护底栏区（Fixed Bottom）**：包含分割线、输入行 `❯ █`、状态行，高度为 $F$（通常 $F = 3$）。
- **滚动安全上界**：$B = H - F$（$H$ 为终端总行数）。

当任务刚刚开始时：$V + \text{动态帧高度} \le H$，空间充足，工具完成时直接在绝对行 $V+1$ 输出，不发生滚屏。

当工具执行多轮后，$V$ 达到 $B$：**“可视窗口被 commit 满了”**。
此时新工具完成，必须推入 scrollback：
`plan_commit`（`commit_output.py`）发出 DECSTBM 硬件滚屏序列：
$$\text{ANSI: } \texttt{\x1b[1;\{B\}r\x1b[\{B\};1H\r\n\x1b[r}$$
该序列将 $[1, B]$ 区域向上滚动 1 行（最顶行进入终端 scrollback），而在第 $B$ 行生成硬件空白行；光标随后被定位到第 $B$ 行写入新提交的工具文本：
$$\text{ANSI: } \texttt{\x1b[\{B\};1H\{tool\_text\}\x1b[K}$$

### 2.2 机制二：双重滚屏竞争（Double Scrolling）

在 `app.py` 中，每一次提交或定时刷新流程为：
```python
commit = self._flush_committed(force=force)  # 1. 提交已完成内容（可能触发滚屏）
self._render_frame()                         # 2. 紧接着重绘动态活动帧
```
在 `_render_frame()`（`render_frame.py`）中：
1. 系统重新计算当前活动帧（Vibe 提示行、Busy 状态、Thinking、Bottom）所需的高度 $\text{frame\_rows}$。
2. 即使提交刚刚完成，`_frame_scroll_plan` 又根据当前屏幕几何检查重叠：
   $$\text{overlap} = V_{\text{after}} + \text{frame\_rows} - H$$
3. 如果此时动态帧由于包含 Busy 提示行（例如 `◐ Crystallizing (21m 7s...)` 占 1 行）导致 $\text{overlap} > 0$，`_frame_scroll_plan` 会**再次计算出需要滚屏，并再次发送滚屏序列**（通过 worker barrier 或直接输出）！
4. **后果**：
   - 刚刚由 `plan_commit` 写入第 $B$ 行的工具行，被 `_frame_scroll_plan` 立即二次向上推滚 1 行；
   - 终端在第 $B$ 行又产生了一个全新的物理空白行；
   - 若动态帧随后的实际绘制起点或裁剪未恰好完全占满该空隙，该物理空白行将直接留在屏幕上。

### 2.3 机制三：活动帧高度抖动（Height Jitter）与物理空白行被推入 Scrollback

截图中为什么空行不是处处出现，而是**间隔几段特定位置才出现 1 ~ 3 行**？
结合日志与会话记录的时间戳和工具类型分析：
1. **段落一到段落二之间**：会话刚初始化与连续多行提交过渡，动态帧从初始较大高度收缩。
2. **段落二到段落三之间**：连续运行短耗时命令（0.04s 的 `./python.py -c ...`）时，Busy Vibe 动画不唤起（或保持同一状态），动态帧高度恒定（仅底栏 $F$ 行），每次提交精确滚 1 行，新行精确覆盖第 $B$ 行，保持无缝。
   - 当切换到耗时较长的 `grep -rn "llm_requests" src/`（耗时 0.31s）以及之后的思考时，`Busy Activity` 定时器唤起，动态帧高度从 3 增至 4（新增 1 行 Vibe）。
   - 为容纳这新增的 1 行，视口触发了向上滚动腾出空间。
3. **段落三到段落四之间**：从长搜索/阅读切回短命令，Busy Activity 结束，动态帧瞬间收缩 1 行。
4. **段落四到段落五之间**：命令执行失败（`exit 2`），产生错误提示并触发状态重置。

**空白行泄漏到 Scrollback 的致命循环**：
1. 动态帧展开时，触发硬件滚屏，终端在屏幕底部产生物理空白行。
2. 动态帧随后收缩，活动区起点下沉或上提，旧行擦除逻辑使用 `\x1b[{row};1H\x1b[K`。
   - **`\x1b[K`（Erase to End of Line）只清空行内的字符内容，不消除该物理行本身（终端中它仍然是一个带有换行的物理行）**。
3. 下一次又有新工具提交而触发下一次硬件滚屏时，**这些原本因高度抖动遗留在视口内的物理空白行，被整行完整推入了终端的 Scrollback**！
4. 最终在 Scrollback 历史中留下了整齐的 1 ~ 3 行永久空白。

---

## 3. 设计不变量与约束 (Invariants)

1. **逻辑与物理连续性不变量**：
   - 紧凑节点（折叠的 `tool_call` 等）在提交到 Scrollback 后，物理历史行之间必须严格相邻，**相邻两工具行在 Scrollback 中的行距必须严格为 1，禁止任何物理空白行介入**。
2. **滚屏原子性不变量（Single Scrolling Authority）**：
   - 在一次完整的事件循环（一次 `_flush_committed` + `_render_frame` 周期）中，**滚屏决议必须是收敛且唯一的**。禁止提交阶段发生一次硬件滚动，紧接着帧重绘阶段又发生二次硬件滚动。
3. **底栏保护不变量**：
   - DECSTBM 滚屏区域必须严格限制在 $[1, H - F]$；底栏 $F$ 行物理位置绝不允许发生非预期的位移或擦写。
4. **收缩空白行吸收不变量**：
   - 动态活动帧收缩腾出的物理行，必须由动态区自身布局吸收或光标紧随上提管理，不得暴露在提交写入区上方，更不得被后续滚屏推入历史。
5. **Worker 几何一致性屏障**：
   - 异步 `worker_mode` 下，已规划提交的几何变化（`scrolled_rows` 与 `next_row`）在提交真正落屏前，必须作为屏障严格约束后续帧的预投影，禁止基于未确认的几何做出错误的滚屏决策。

---

## 4. 架构设计与修复方案 (Architecture & Plan)

```
[ Dock / OutputTree ] 
          │  (新工具完成)
          ▼
[ 统一提交与滚屏协调器 (Commit-Render Scrolling Coordinator) ]
    │
    ├── 1. 统一几何预决议：
    │      计算 (待提交行数 L) + (当前动态帧实际所需行数 D)
    │      在整个视口中一次性规划总滚动行数 S_total = max(0, V_before + L + D - H)
    │
    ├── 2. 单点滚屏执行：
    │      由 plan_commit 一次性执行 S_total 滚动，并将所有 L 行新提交紧密写入
    │
    └── 3. 帧重绘紧随接续 (Zero-Scroll Frame Redraw)：
           _render_frame 获知 S_total 已被提交阶段消费，此时 V_after + D <= H
           _frame_scroll_plan 的 overlap 必为 0，不再触发二次滚屏！
```

### 4.1 方案详解

#### 1. 消除双重滚屏：提交时预留动态帧预算
当前缺陷在于 `plan_commit` 只管提交行 $L$，滚完后留给动态帧的空间可能只有 $H - V_{\text{after}}$；而动态帧发现空间不足又滚一次。
- **改进**：`app.py::_flush_committed` 在调用 `plan_commit` 时，传入动态帧的**最小保底行数**（即底栏行数 $F$ 加上若存在活跃 Vibe/Thinking 时的必要行数）。
- 让提交阶段一次性滚到位，或在动态帧不需要额外滚屏的前提下严禁二次触发 DECSTBM。

#### 2. 收缩帧消除未认领物理空行
- 当动态活动帧高度收缩（例如 Busy Activity 结束减少 1 行）时，物理帧采用顶对齐紧跟已提交历史末尾。
- 视口底部空出的行，若尚未被硬件滚出屏幕，仅作为动态区下方的可见终端空白；在后续下一次需要提交时，**优先复用屏幕内现存的这些空行空间写入新内容，直到真正触及底栏保护边界前，绝不提前发起硬件滚屏**。

#### 3. 严格校验 `_frame_scroll_plan` 与 `plan_commit` 滚屏边界
- `_frame_scroll_plan` 在没有新内容超出时，返回的 `scroll_rows` 必须严格为 0。
- 当 `fixed_bottom_rows > 0` 时，滚动区域顶底界 `1;{scroll_bottom}r` 必须精确匹配 `H - fixed_bottom_rows`，并且滚屏后光标定位必须落在有效写入行。

---

## 5. 详细实现任务与改动点

### 5.1 `tui/voidx_cli/render_frame.py`：滚屏重叠计算与提交抑制
1. **二次滚屏拦截**：
   - 检查 `_render_frame` 中的 `_frame_scroll_plan`。在同一调度周期内，如果刚完成提交且提交已处理了历史滚动，检查 `visible_after + frame_rows <= term_height`；若动态帧已在可用区域内，确保 `scroll_ansi == ""`。
2. **动态区伸缩平滑**：
   - 确保 `busy_activity_rows` 的增减不会使 `_last_frame_rows` 产生虚假的溢出判定。
   - 当 `busy_activity_rows` 从 1 变为 0 时，重绘仅在原物理行内擦除，不修改 `visible_committed_rows`。

### 5.2 `tui/voidx_cli/commit_output.py` 与 `commit_geometry.py`
1. **提交写入紧密性**：
   - 核验 `plan_commit`：当多行或单行提交写入时，写入序列为：
     `\x1b[{row};1H{line}\x1b[K`
     对于连续提交，下一个 `start_row` 严格等于上一次的 `next_row`，中间不允许跳行。
2. **滚屏行数与 `next_row` 绝对一致性**：
   - 确保 `V_after = min(B, max(0, V_before + lines_written - scrolled_rows))` 与 `output.next_row - 1` 完全相等。

### 5.3 `tui/voidx_cli/app.py`：调度周期状态收敛
1. **合并提交与重绘的几何通知**：
   - 在 `_flush_committed` 成功后，同步更新 `self._visible_committed_rows`，并在紧随其后的 `_render_frame()` 中直接使用该最新确认为基准，不得读取旧快照。
2. **Worker Mode 屏障收敛**：
   - 在 Worker 模式下，未完成的提交 token 必须严格阻断下一帧的独立滚屏，直到提交的物理结果被应用。

---

## 6. 测试与验证策略 (Test & Verification Strategy)

### 6.1 验收测试矩阵

测试基于 `tui/tests/test_layout_terminal_model.py` 的真实 `_VTScreen` 物理模型进行驱动，通过比对虚拟终端的 `screen.rows` 和 `screen.history` 进行像素级/字符级断言。

| 编号 | 测试场景 | 断言要求 | 测试文件 |
| :--- | :--- | :--- | :--- |
| **TC-01** | **视口刚好占满时的连续工具提交** | 设定 $H=10, F=3$（保护界 $B=7$）。提交 7 个单行工具填满视口后，再连续提交 5 个单行工具。验证 `screen.history` 与 `screen.rows` 中所有工具行完全连续，**行与行之间空行数为 0**。 | `tui/tests/test_layout_terminal_model.py` |
| **TC-02** | **满载状态下 Busy Activity 启停抖动** | 视口已满状态下，工具执行中唤起 1 行 Busy 提示（动态帧 $3 \to 4$ 行），工具结束提示消失（$4 \to 3$ 行）并提交工具结果。验证在提示出现与消失的交替过程中，**Scrollback 历史中无残留空行**。 | `tui/tests/test_layout_terminal_model.py` |
| **TC-03** | **工具执行失败/报错分支交替** | 满载状态下，正常工具与 `exit 2` 失败工具交替提交。验证失败节点的红色标头与正常节点紧凑排列，段间无空白行。 | `tui/tests/test_layout_terminal_model.py` |
| **TC-04** | **Worker 异步模式下的提交与帧滚屏** | 开启 `worker_mode`，连续排队提交与帧刷新，验证提交与帧渲染不会发出两次重叠滚屏序列。 | `tui/tests/test_frame_advanced.py` |

### 6.2 验证命令

```bash
# 运行 TUI 终端物理模型针对性测试
./test.py --backend -- tui/tests/test_layout_terminal_model.py -v

# 运行 TUI 渲染与帧高级测试
./test.py --backend -- tui/tests/test_frame_rendering.py tui/tests/test_frame_advanced.py -v

# 运行全量 backend 回归测试
./test.py --backend
```
