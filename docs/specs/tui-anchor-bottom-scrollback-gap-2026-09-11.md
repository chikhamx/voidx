# TUI 动态区悬空与提交空行累加修复技术规格

> **状态**：设计完成，等待评审与实施
> **日期**：2026-09-11
> **适用范围**：`tui/voidx_cli/` 的视口物理投影（`layout.py`）、提交规划与起点决策（`app.py`、`commit_output.py`）、底端锚定生命周期
> **受众**：Human + LLM
> **关联规格**：[视口溢出与终端残影](tui-viewport-overflow-rendering-2026-09-11.md)、[提交瞬态清理与帧对齐](tui-commit-transient-cleanup-2026-09-11.md)

---

## 1. 问题与证据边界

### 1.1 现场截图现象与行号还原

用户在真实终端运行中反馈两个强相关的空行异常：
1. **现象一：动态区与 Scrollback 之间出现多行空白悬空**
   - 截图中终端高度为 16 行：
     - **第 1 ~ 3 行（物理行 1..3）**：已提交到 scrollback 并在可见屏幕内的历史消息（`37 "smallest_change",` 及一条占两行的用户/代理消息，`visible_committed_rows = 3`）。
     - **第 4 ~ 6 行（物理行 4..6）**：完全空白无内容（红框标注区域）。
     - **第 7 ~ 16 行（物理行 7..16）**：当前动态区，总高 10 行：
       - 第 7 行：未完成工具 `● Bash("./test.py --backend")`（transcript 区域）
       - 第 8 行：动态提示 `◐ Crystallizing (21m 7s...)`（vibe 区域）
       - 第 9 ~ 13 行：Todo 面板（todo 区域，共 5 行）
       - 第 14 ~ 16 行：分割线、输入框 `❯ █`、状态栏（bottom 区域，共 3 行）
   - **目标行为**：动态区总行数为 10 行，可用空间为 13 行，应从第 4 行连续绘制至第 13 行，底部第 14..16 行留白。当前整帧被推至第 7 行，导致历史之后出现第 4..6 行的布局空白。

2. **现象二：工具执行间隙频繁多出数行永久空行**
   - 截图中 `Read("tui/voidx_cli/app.py")` 与 `Read("tui/voidx_cli/layout.py")` 之间，以及后续命令之间，留下了整齐的约 3 行空行。
   - 在多次工具执行或思考流展开/收缩后，这些空行不是暂时的渲染间隙，而是被固定记录在了终端的 scrollback 历史中。

### 1.2 证据与本次结论的限度

截图行号是现场现象的还原，不是本次复现结果。§2 的下沉与提交起点代码已静态核对，能够解释该故障机制；尚不能据此认定截图中的所有永久空行都来自同一原因。实施必须先用 §6 的失败测试确认链路，并区分源内容 spacer 与布局插入空行。

另有相关状态风险：提交规划已经返回 `scrolled_rows`，而当前三条提交成功路径仍只累加可见历史行数；本规格一并修正该结算，避免将不准确的计数升级为起点权威。代码行号为修订时定位参考，实施以函数名为准。
---

## 2. 根因分析与代码事实

### 2.1 根因一：`anchor_bottom` 整帧下沉导致物理悬空

位于 `tui/voidx_cli/layout.py` 第 590~595 行：

```python
effective_start_row = frame_start_row
if anchor_bottom:
    total_rows = top_rows + bottom_rows
    if total_rows <= terminal_height:
        effective_start_row = max(frame_start_row, terminal_height - total_rows + 1)

bottom_start_row = effective_start_row + top_rows
```

1. **下沉触发链**：
   - 终端在运行过程中，一旦曾有长内容使底栏触及终端底部（`self._last_bottom_start_row + self._last_bottom_rows - 1 == term_height`），`_bottom_dock_is_anchored` 变为 `True`。
   - 随后 `render_frame.py`（第 539、582 行）在没有临时面板时，持续将 `anchor_bottom=True` 传入 `_physical_viewport_for_frame`。
2. **整帧被推迟**：
   - 当动态内容从多行收缩为较少行（如 Thinking 结束、或工具执行中只有单行概要时），`total_rows = 10`。
   - `effective_start_row = max(4, 16 - 10 + 1) = 7`。
   - `layout.py` 将 `effective_start_row` 赋值给返回的 `PhysicalViewportPlan.frame_start_row`。
   - 渲染系统从第 7 行开始绘制 transcript（`● Bash(...)`）。
   - **第 4 ~ 6 行未被任何视口切片（source slice）认领，直接作为物理空白暴露在屏幕上**。

### 2.2 根因二：提交起点使用下沉后的 `last_frame_start_row` 导致跳行固化

位于 `tui/voidx_cli/app.py` 第 1561~1565 行：

```python
clear_start_row = (
    self._last_frame_start_row
    if self._has_rendered_frame and self._last_frame_start_row > 0
    else self._visible_committed_rows + 1
)
```

1. **提交起点错位**：
   - 当工具完成，dock 触发 `_flush_committed()` 时，上方的 `self._visible_committed_rows` 为 3。真实的已提交历史末尾是第 3 行，下一行提交应该写入第 4 行。
   - 但代码优先取了 `self._last_frame_start_row`。由于上一帧被 `anchor_bottom` 推到了第 7 行，`clear_start_row` 被赋予了 `7`。
2. **跳过物理行并固化为空白**：
   - `plan_commit` 收到 `start_row = 7`，直接通过 `\x1b[7;1H...` 在第 7 行写入本次提交的内容。
   - **第 4 ~ 6 行被完全跳过，从未被擦除或写入，永久留在终端屏幕上**。
3. **行数统计与物理坐标脱节累加**：
   - 提交完成后，`app.py` 执行：
     ```python
     self._visible_committed_rows = min(
         term_height,
         self._visible_committed_rows + update["flush_rows"],
     )
     ```
   - 若本次提交了 1 行，`_visible_committed_rows` 从 3 增至 4。
   - 但物理上刚写入的那一行位于第 7 行！
   - 下一次渲染时，`start_row = visible_after + 1 = 5`，而实际上第 7 行已有提交文本；随后如果再次满足 `anchor_bottom` 下沉条件，动态区再次向下漂移，下一次提交再次跳过数行。
   - 如此往复，每次工具或消息在下沉状态下提交，都会在已提交内容和新提交内容之间插入一段“悬空空白”，随着后续硬件滚屏推入 scrollback，形成了截图中工具间断性多出几段空行的现象。

---

## 3. 已确定的设计与不变量

### 3.1 唯一布局策略：连续顶对齐

- 整个活动帧紧随可见历史，底栏紧随顶部内容；收缩后底栏和光标一起上提，旧尾部留白。不实现底栏单独贴底，不增加帧内填充行，不新增配置。
- 对所有会话状态采用同一策略，包括初次启动、清屏后和历史全部滚出可见屏幕后。`visible_committed_rows == 0` 不等于会话没有历史，也不构成下沉例外。
- 设 `V` 为当前已确认的可见历史行数（包含合法的语义空行）。渲染若需滚屏，先由 `_frame_scroll_plan` 得到 `V_after`；物理帧从 `V_after + 1` 开始。无滚屏、提交或 resize 时，单纯收缩不得改变帧起点。
- `transcript` 非空时从帧起点开始；为空时不人为插入空行，由后续首个非空区域接续。帧内仍保留源内容本身的语义空行。
- `anchor_bottom` 不再影响物理投影起点。保留现有参数兼容调用，但两种参数值产生相同投影；底端是否触底仍用于判断当前操作是否拥有受保护滚屏区域。

### 3.2 提交起点与结算

提交规划使用操作开始前的已确认几何快照：

- `H`：终端高度；`F`：本次已授权保护的底栏行数；`B = H - F`。
- `V_before`：提交前可见历史行数；`start_row = V_before + 1`，不得使用旧帧起点代替。
- `L = output.lines_written`，`S = output.scrolled_rows`。
- 成功应用提交后：`V_after = min(B, max(0, V_before + L - S))`，并必须满足 `V_after == output.next_row - 1`。测试同时检查两种表达，避免单纯截断掩盖规划错误。
- 下一次提交以这个已确认的 `V_after + 1` 为起点；下一帧可以为自身腾出空间再次滚屏，但必须基于同一计数重新规划，不能重复扣减本次 `S`。

`start_row` / `next_row` 是逻辑规划坐标，可以暂时等于 `B + 1`（无底栏时可为 `H + 1`）；不得直接生成越界 CUP。保持 `plan_commit` 的既有延期规则：没有已授权的受保护滚屏区而提交溢出时返回 `None`，不消费提交批次、不更新几何，并请求后续帧/重试。不得通过把起点钳到 `B` 覆盖历史。

### 3.3 已应用状态与在途状态分离

- 同步路径在 `write`、`flush` 成功后统一结算；Worker 有 `wait` 时在 token 确认成功且 `restore_epoch` 仍匹配后结算。
- Worker 无 `wait` 的兼容分支维持现有提交调用成功返回即结算的契约；这不代表新增真实异步确认能力。测试必须单独覆盖该分支。
- 规划或排队不得提前修改已应用的 `_visible_committed_rows`、`_last_frame_start_row`、`_last_frame_rows`。计划几何保存在 pending update 中。
- 三条成功路径复用同一几何结算逻辑，禁止继续各自执行 `min(term_height, visible + flush_rows)`。
- 保持现有 pending token 串行屏障。失败、取消、过期 epoch 不应用计划几何，并保留既有批次恢复、guidance 恢复和缓存失效机制；部分物理写入失败不承诺终端自动回滚。

---

## 4. 实施任务与代码变更

### 4.1 `tui/voidx_cli/layout.py`：去掉整帧下沉

- `project_physical_viewport` 保持传入的 `frame_start_row`；删除 `anchor_bottom` 对 `effective_start_row` 的位移计算。
- 保留连续帧模型：`bottom_start_row = frame_start_row + top_rows`，`frame_rows = top_rows + bottom_rows`。不修改源切片、裁剪优先级与光标映射规则。
- 先修改 `tui/tests/test_layout.py` 的 `test_physical_viewport_anchor_bottom_unfilled_terminal_aligns_frame_start`：现有输入的预期改为帧起点 1、底栏起点 3，而非 6、8；补充起点 4 和 `anchor_bottom=False/True` 等价性测试。满屏自然触底测试保留。
- 验证命令：`./test.py --backend -- tui/tests/test_layout.py -v`。

### 4.2 `tui/voidx_cli/commit_output.py`：提交范围与旧帧范围解耦

为 `plan_commit` 增加可选关键字参数 `previous_frame_start_row: int | None = None`，与既有 `previous_frame_rows` 一起描述旧帧拥有的物理范围。省略起点时沿用 `start_row`，保持既有调用兼容；零高度表示没有清理所有权。`app.py` 必须显式传入已应用旧帧起点。

设旧范围为 `[P, P + N - 1]`，本次滚屏区域为 `[1, B]`：

1. 先将旧范围与屏幕 `[1, H]` 相交；禁止把两起点之间的未知空隙当作旧帧所有权。
2. 对旧范围内 `r <= B` 的行，滚屏后位置为 `r - S`，小于 1 的行已滚出；`r > B` 的受保护底栏保持原位。
3. 提交清理只针对映射后仍属于旧帧、且处于 `[output.next_row, B]` 的行。新提交覆盖的行由写入时 EL 处理，受保护底栏留给下一帧更新。
4. 不得再用 `start_row + previous_frame_rows - 1` 代替显式旧尾端，也不得使用无范围 ED 擦掉受保护底栏。

新增“旧帧 7..16、新提交从 4 开始”的用例：旧顶部尾行必须清理到授权上界，历史 1..3 和保护底栏保持不变。另测跨滚屏区域的旧帧：上部平移，底栏不平移。

测试放在 `tui/tests/test_layout_terminal_model.py`，复用现有 `test_commit_respects_explicit_applied_ownership`、`test_commit_owned_envelope_maps_through_scroll` 的模型设施。验证命令：`./test.py --backend -- tui/tests/test_layout_terminal_model.py -v`。

### 4.3 `tui/voidx_cli/app.py`：统一提交事务几何

- `_flush_committed` 在规划前快照 `V_before`、终端高度、保护区域和旧帧范围，以 `V_before + 1` 规划提交。
- 按 §3.2 计算待应用几何，将实际 `output.next_row` 和滚屏结算结果传给 `_track_pending_commit` / `_wait_pending_commit`，不再通过 `output.next_row - flush_rows` 反推出提交前坐标。
- 将现有写入前更新 `_last_frame_*` 的逻辑移到成功结算阶段；成功后基线起点采用 `output.next_row`，不能再用 `max(old_start, next_row)` 阻止合法上移。
- 旧帧剩余包络依据 §4.2 映射后的旧范围计算，不以新提交起点推导旧尾端。保留底栏时保留其真实物理坐标；缓存不再代表实际像素时失效布局快照，由下一帧重绘，不能为了保留缓存而改动已确认历史计数。
- 同步与 Worker 的基线保留语义必须与 `terminal_writer.py` 的 positioned commit 一致；复用现有 writer 协议，不引入新协议字段。若测试发现必须改协议才能满足不变量，停止实施并补充评审，不静默扩大范围。
- 测试放在 `tui/tests/test_frame_advanced.py`，覆盖有等待、无等待和同步结算，以及失败/取消/过期 epoch 不应用几何。
- 验证命令：`./test.py --backend -- tui/tests/test_frame_advanced.py tui/tests/test_terminal_writer.py tui/tests/test_tui_cleanup_failure_acceptance.py -v`。

### 4.4 `tui/voidx_cli/render_frame.py`：同步与 Worker 渲染联动

- 同时检查 `_render_frame` 中同步和 Worker 两条路径，不能只修改 Worker 分支。
- `_frame_scroll_plan` 的 `visible_rows` 使用操作前已确认计数，最终投影使用返回的 `visible_after + 1`。预投影与最终投影都不得再因 `anchor_bottom` 下沉。
- 受保护滚屏边界必须来自操作前实际触底底栏的物理位置，即 `H - F`；不得使用取消下沉后预投影的 `provisional.bottom.region.start_row - 1` 作为旧屏幕滚屏边界。
- 收缩无滚屏时清理旧帧尾部并上提底栏；新帧成功应用后按实际底栏位置重新判断锚定，不能把“曾经触底”当作永久状态。
- 底栏上提后若提交溢出且无保护区，沿用 §3.2 的延期机制；验证后续渲染能建立合法空间/保护区并最终完成提交，不得卡在无限重试。
- 测试放在 `tui/tests/test_layout_terminal_model.py` 和 `tui/tests/test_frame_rendering.py`；验证命令：`./test.py --backend -- tui/tests/test_layout_terminal_model.py tui/tests/test_frame_rendering.py -v`。

---

## 5. 禁止改动与边界约束

1. 不修改 Dock/Tree 的节点生命周期、`collapsed` 状态、工具结果内容或 `_ensure_result_spacer`。合法语义空行不属于本次修复目标。
2. 不删除受保护底栏的硬件滚屏机制；滚屏授权依据旧的已应用物理几何，而非新帧期望位置。
3. 尾部清理不得进入该操作滚屏后的已提交历史保护区。提交写入行上的 EL 只用于清除该新提交行残留，不得把它误判为禁止写入历史。
4. 不重建或擦除既有 scrollback，不尝试修复升级前已永久写入的空行。本次保证从可信连续历史基线开始不再产生额外布局空行；旧动态帧偏移仍须按 §4.2 安全清理。
5. 不修改终端输入、协议、非 TTY 输出或 Alt-Screen 行为，不引入新的第三方依赖。

---

## 6. 验证标准与完成条件

### 6.1 失败测试先行与验收矩阵

每个实现任务先运行新增/更新的针对性测试，确认因目标缺陷而 RED，再实现并确认 GREEN。仅更新旧断言不能替代新故障回归。

| 场景 | 必须断言 | 测试文件 |
| --- | --- | --- |
| 16 行屏幕收缩 | 历史 1..3；旧帧 4..16；新帧 6 行占 4..9；10..16 无旧残影；历史不变，光标随底栏上提 | `tui/tests/test_layout_terminal_model.py` |
| 零历史与锚定参数 | 初始、清屏后、历史滚出后三种场景均从 1 开始；短帧两种 anchor 参数投影一致 | `tui/tests/test_layout.py`、`tui/tests/test_layout_terminal_model.py` |
| 旧帧起点偏移 | 旧帧 7..16、提交起点 4；旧范围尾部正确清理，未拥有行和保护底栏不误擦 | `tui/tests/test_layout_terminal_model.py` |
| 提交滚屏结算 | `H=6,F=1,V_before=3`，提交 `A\nB\nC`：`S=1,next_row=6,V_after=5`；下一帧从此状态规划，不重复计入提交滚屏 | `tui/tests/test_layout_terminal_model.py`、`tui/tests/test_frame_advanced.py` |
| 连续工具 | Read、Read、Bash 的实际提交序列与预期 Dock 行序列一致，保留 spacer 和结果行，无额外布局空行 | `tui/tests/test_layout_terminal_model.py` |
| 异步确认 | 确认前已应用几何不变；成功后恰好结算一次；失败、取消、过期 epoch 不应用计划几何 | `tui/tests/test_frame_advanced.py` |
| 保护边界与延期 | 收缩前保护区仍取旧底栏边界；收缩后解除锚定；无保护溢出不输出，后续可恢复提交；极小终端无越界 CUP | `tui/tests/test_layout_terminal_model.py`、`tui/tests/test_frame_rendering.py` |

端到端收缩和连续提交测试均覆盖同步 writer 与真实 Worker 模型路径，复用 `_VTScreen` / `_ModelStream`，显式等待 Worker drain/确认后检查最终屏幕；另以兼容 stub 覆盖无 `wait` 分支。

连续工具测试必须先构造足够历史，再追加提交与渲染直到目标工具哨兵进入 `screen.history`，先断言 history 非空且包含目标哨兵，再比较 `screen.history + screen.rows` 中目标提交区间与预期源行序列。禁止通过过滤所有空行、只比较工具标题或检查空 history 来获得通过。每次提交记录操作前历史末尾及 `scrolled_rows`，在同一滚屏坐标系中校验连续性。

### 6.2 自动化测试命令

```bash
# 1. 布局投影和终端物理模型
./test.py --backend -- tui/tests/test_layout.py tui/tests/test_layout_terminal_model.py -v

# 2. 提交事务、渲染双路径、writer 基线和失败恢复
./test.py --backend -- tui/tests/test_frame_advanced.py tui/tests/test_frame_rendering.py tui/tests/test_terminal_writer.py tui/tests/test_tui_cleanup_acceptance.py tui/tests/test_tui_cleanup_failure_acceptance.py -v

# 3. 完整后端与 TUI 回归
./test.py --backend
```

### 6.3 完成判据与证据记录

- §6.1 全部用例具备目标失败原因的 RED 和实现后的 GREEN 记录；§6.2 的专项与完整回归在最终实现状态通过。
- 记录测试命令、执行环境和结果。终端模型通过只证明该模型覆盖的 ANSI 行为；真实终端复测须另记终端类型、尺寸及收缩/连续提交结果，不以模型结果冒充截图现场复现。
- 复核源切片、语义 spacer、历史保护和失败恢复均未改变；文档所列实现文件实际存在且功能验证通过后，才可按项目归档流程归档。本次文档修订不代表实现完成或测试已通过。
