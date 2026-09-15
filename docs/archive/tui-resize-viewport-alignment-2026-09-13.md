# TUI 满载视口高度收缩后的历史边界校正规格

> **Status: Done** — 已实现并验收：模型 + 真 PTY 回归全绿，Ghostty 裁剪语义逐行对齐固定提交源码；实机窗口行为确认留作后续人工核对。Archived on 2026-09-16.

- **创建日期**：2026-09-13
- **修订日期**：2026-09-14
- **状态**：已完成。高度收缩校正已实现并通过全部模型与真 PTY 回归；Ghostty 裁剪语义已逐行对齐固定提交源码；实机窗口行为确认留作后续人工核对（不阻塞归档）。
- **受众**：Human + LLM
- **范围**：主屏幕模式，宽度不变、仅缩小高度；重点是已有提交历史、窗口满载、子代理与 Wait 仍在动态区
- **关联规格**：
  - [TUI 视口满载提交与动态区伸缩空行累加修复规范](tui-viewport-full-commit-scrollback-gap-2026-09-12.md)
  - [TUI 动态区悬空与提交空行累加修复技术规格](../archive/tui-anchor-bottom-scrollback-gap-2026-09-11.md)
  - [TUI 提交瞬态行残留清除与帧对齐技术规格](../archive/tui-commit-transient-cleanup-2026-09-11.md)

## 1. 决策与目标

需要修复的是：**高度收缩使物理内容上移后，TUI 仍使用旧的可见历史行数计算滚屏与重绘起点，可能重复滚屏、留下旧动态节点，甚至将其推进历史。**

先补充真实终端证据和正式失败测试，再确定边界校正实现。不是重做容量裁剪，也不是简单增加全屏擦除。

目标：

1. resize 后，在决定应用滚屏与擦除起点之前，建立可信的可见历史／动态区边界。
2. 不因应用错误滚屏新增动态内容的历史污染；不在可见区留下旧动态帧。
3. 已提交历史在完整缓冲中保持原有顺序，不丢失、不重复。
4. 保持短内容与底栏紧凑连续排列，以及现有容量和输入光标保障。

不承诺所有终端 resize 后历史中绝无动态旧行。必须区分终端自身调整缓冲造成的变化与应用后续输出造成的污染；普通 `CUP + ED` 无法回收已经进入历史的行，也不得通过清空用户历史伪造达标。

## 2. 现场与验证证据

### 2.1 用户确认的场景

- 终端为 Ghostty，具体版本尚未取得。
- 宽度不变，只拖动缩小高度。
- 已有提交历史，窗口处于满载状态。
- 子代理仍在运行，子代理节点与 Wait 位于未提交动态区。
- 现场表现为多个时间切片的子代理标题与空行残留，部分残留进入回滚历史。

先前“无历史、未满窗口”的实验不代表这个场景。现场是否存在其他并发输出因素，仍需实机记录确认；不能仅凭截图断言 OutputTree 重复或完全排除其他原因。

### 2.2 当前代码中的失配路径

以下位置按本次修订时源码核对，后续以符号为定位依据：

| 位置 | 当前行为及影响 |
|---|---|
| `tui/voidx_cli/render_frame.py::_apply_resize_state` | 仅调用 `_invalidate_frame_cache()`，不校正 `_visible_committed_rows` |
| 同文件 `_render_frame` worker 路径（约 577 行）及同步路径（约 742 行） | 读取旧 `_visible_committed_rows`，交给 `_frame_scroll_plan` 计算新布局 |
| 同文件 `_frame_scroll_plan` | 根据可见历史行数、帧高、新终端高度决定滚屏量；裁到新高度不能代表真实边界恢复 |
| 同文件 `_render_frame` | 用 `visible_after + 1` 决定绘制起点，错误计数会留下起点以上的旧动态行 |
| `tui/voidx_cli/terminal_writer.py::_write_frame_full` | 从指定起点发出 `CUP + ED`，无法清理起点以上的盲区或回滚历史 |

此路径已在受控模型中导致残影，但目标 Ghostty 版本上的完整因果链尚未实机确认。

### 2.3 已有保障，不作为新增功能

- `layout.py::project_physical_viewport` 已按容量给底栏和动态区域分配空间，并保留 Transcript 尾部。
- `layout.py::project_bottom_viewport` 已优先保留光标所在输入窗口，在小高度下裁剪其余底栏内容。
- `_render_frame` 已将 `resize_frame` 纳入 `force_full`。
- `TerminalWriter.submit_barrier` 已对 `kind="resize"` 自动使帧失效，不要求调用方再显式设置 `invalidate_frame=True`。
- `TerminalWriter._process_frame` 已用 synchronized output 序列包围帧输出。合并为一次 write 不等于终端原子显示，不能承诺彻底消除闪烁。

### 2.4 Ghostty 上游证据的边界

核对固定提交 `7aab0a0392369613472bd5dcfd66bef58e78c3ec` 的 [Screen.zig 测试](https://github.com/ghostty-org/ghostty/blob/7aab0a0392369613472bd5dcfd66bef58e78c3ec/src/terminal/Screen.zig#L8176-L8198)：`Screen: resize less rows with empty scrollback` 将 3 行缩为 1 行后，可见区只剩末行，完整缓冲仍保留原 3 行。

这证明上游预期包含“高度收缩保留顶部内容至历史”的行为；本次只读取源码与测试，没有本地执行 Ghostty Zig 测试。它不等于已确认用户安装版本，也不足以确定满载 TUI、光标下方有底栏时的全部裁剪规则。

### 2.5 受控模型实验

通过 `./python.py` 内存脚本调用现有 `_terminal_tui`、`DockEventConsumer`、真实 `_render_frame` 与 worker writer。未启动真实耗时子代理，子代理和 Wait 由事件构造。脚本未落为正式测试，以下结果是调查记录，不是可复用的最终验收证据。

初始状态：宽 80、高 24；创建 35 个已完成历史节点并实际提交，再添加活动子代理、读取工具与 Wait；首帧检查确认可见历史 14 行，动态帧（含底栏）10 行，输入光标在第 22 行。模型先裁光标下方空间，再将超出新高度的顶部行移入历史。这个策略仅用于暴露边界失配，不是生产位移算法。

每个目标高度从同一初始状态独立运行：

| 高度变化 | 模型 resize 自身推入历史 | 应用重绘后的观测 |
|---|---:|---|
| 24 → 20 | 2 行，均为已提交历史 | 可见区两份子代理标题 |
| 24 → 16 | 6 行，均为已提交历史 | 可见区两份子代理及 Wait |
| 24 → 11 | 11 行，均为已提交历史 | 应用后续滚屏将旧子代理、Wait、活动行和部分底栏推进历史，下方又绘制新帧 |

以 24 → 20 为例：resize 后实际可见历史为 12 行，TUI 仍按 14 行计算并滚动 4 行；实际只剩 8 行历史，但记录为 10 行，新帧从第 11 行开始，旧标题留在第 9 行。

实验中 `_assert_applied_screen` 仍通过，因为它主要检查新帧范围及其下方空白，不验证新起点以上究竟是历史还是旧动态行。正式回归必须补齐全屏与历史断言。

## 3. 修复契约与实现前置条件

### 3.1 目标处理顺序

```text
检测到高度变化
→ 确认最后实际输出状态与新尺寸的关系
→ 校正可见历史／旧动态区的物理边界
→ 基于校正边界计算必要滚屏
→ 在有序输出中清理旧动态区并绘制新帧
→ 输出成功后发布对应几何状态
```

生产方案定稿前必须解决：

1. **边界依据**：目标终端如何裁减光标下方空间、移动顶部内容，以及如何可靠确定这些变化。记录支持条件；不能将模拟公式直接视为通用终端协议。
2. **输出基准**：使用最后实际完成的终端输出状态，而不是尚未完成的计划帧。期间的 commit、光标局部更新及 barrier 是否改变基准，都必须核对。
3. **连续 resize**：处理旧尺寸帧在途、多个尺寸变化合并和 barrier 完成顺序，避免重复扣减同一次位移。
4. **不确定状态**：明确无法可靠恢复边界时的处理策略及历史保护方式。未解决前保留阻塞状态，不得猜坐标、清空历史或静默继续使用旧计数。
5. **同步路径一致性**：worker 与同步输出都必须遵守同一边界契约，避免两套不同的推导规则。

这些问题未定稿前，仅允许继续证据收集及回归模型设计，不据此直接实现生产校正算法。

### 3.2 不变量与禁止变更

- 每次应用滚屏前，必须确认将被推出的行属于可安全提交／保留的历史，不能把活动节点或底栏误认为历史。
- resize、清理与新帧输出必须遵守现有 writer 排序及完成状态发布规则；清理不得与其他批次任意交错。
- 擦除范围覆盖仍可见的旧动态内容，但不得越过可信历史边界擦除用户历史。
- 所有物理坐标与投影行数保持在新视口范围内，沿用现有容量裁剪和输入窗口策略。
- 无可见历史时仍从第 1 行紧凑绘制；短内容不强制把底栏推到底部。
- 禁止简单把 `_visible_committed_rows` 置零，或直接减去高度差作为通用修复。
- 禁止清空 Scrollback、切换 alternate screen、重播全部历史或修改 OutputTree 提交语义来掩盖残影。
- 禁止重复实现已有 resize 失效与容量裁剪机制。
- 本次不引入节点结构化折叠、不调整 Todo／Thinking／Vibe 优先级、不重设计极小窗口底栏。
- 普通行间换行允许；禁止的是导致动态内容意外溢出滚屏的输出，而不是所有 `\n`。
- 宽度变化及 reflow 不在本次修复范围，不能顺带替换已有处理；已有相关回归必须保留。

## 4. 文件职责与后续实施边界

下表是定稿后应核对的职责，不要求修改所有文件。新增状态字段、接口和位移策略须在第 3.1 节问题解决后补充，并单独确认实施计划。

| 文件 | 职责 |
|---|---|
| `tui/voidx_cli/render_frame.py` | 主要修复入口：resize 边界恢复、滚屏计划、旧动态清理及同步／worker 状态应用 |
| `tui/voidx_cli/terminal_writer.py` | 核对 barrier、在途帧、成功回执与输出顺序；仅在证据证明需要时修改 |
| `tui/voidx_cli/state.py` | 若需要新几何状态，明确其来源、有效期、失效条件及唯一写入方 |
| `tui/voidx_cli/app.py` | 核对 `_apply_commit_geometry` 与 resize 期间提交的状态衔接，不改提交语义 |
| `tui/voidx_cli/layout.py` | 保持现有有界投影；不承担节点级折叠或猜测终端物理位移 |
| `tui/tests/test_layout_terminal_model.py` | 明确支持的 resize 模型，补全整屏、历史和光标检查 |
| `tui/tests/test_tui_cleanup_acceptance.py` | 构造有历史、满载、活动子代理和 Wait 的真实 consumer 渲染链路；现有 `_resize` 是左上角裁剪模型，不应静默更换语义 |
| `tui/tests/test_frame_advanced.py` | 状态发布、连续 resize 与在途操作的定向回归 |
| `tui/tests/test_terminal_writer.py` | 仅在 writer 有改动时增加对应排序／失效回归 |

## 5. 验证与验收

### 5.1 首先固定复现场景和阶段边界

将第 2.5 节实验转为正式参数化回归，以 `worker=False/True` 覆盖两条输出路径。断言前置状态确为满载且有可见提交历史；不以 mock 直接伪造最终几何状态。

对每次 resize 记录三个阶段的完整可见区、历史及光标：

- **A**：resize 前最后完成输出；
- **B**：只执行终端模型尺寸变化，尚无新应用输出；
- **C**：应用处理新尺寸后的输出完成。

分别解释 A→B 的终端行为与 B→C 的应用行为，不用历史总行数为零作为验收条件。模型的 resize 规则必须独立验证并标注适用范围；保留现有左上角裁剪模型作为不同语义，不把某一种模型冒充所有终端。

### 5.2 必需矩阵

| 场景 | 核心断言 |
|---|---|
| 满载、有历史，分别 24→20、24→16、24→11，宽 80 | 整个可见区无旧标题／Wait 副本；B→C 不把活动节点及底栏推进历史 |
| 同一会话连续缩小与重复相同尺寸 | 不累计位移误差，不重复校正或新增污染 |
| resize 时旧帧或提交尚在途 | 完成回执与几何对应，不应用过期边界；需确定性控制时序，不靠 sleep 碰运气 |
| 有历史但窗口未满 | 正确利用终端实际保留的空间，不额外滚动历史 |
| 无可见历史、短内容 | 第 1 行开始，底栏紧随内容，不新增强制填充 |
| 动态内容超高、高度 1～4 | 沿用有界投影，光标有效，不因修复引入坐标越界 |
| 子代理完成／Wait 结束后的提交 | 历史保持正确顺序，无重复提交或永久遗留活动副本 |

所有场景检查：

1. 用唯一内容标记而非颜色或子代理显示名计数；裁剪允许隐藏标题，但不得出现旧副本。在确认标题应完整可见的目标场景中要求恰好一次。
2. 比较 `screen.history + screen.rows` 中已提交历史标记的有序序列，确保不丢失、不重复。
3. 检查新帧起点以上区域确实是剩余历史，而非只验证 `_assert_applied_screen` 的帧内区域。
4. 检查 `screen.history` 在 B→C 新增的内容，而非只比较历史长度；应用为新帧腾空间时允许滚动真正的历史。
5. 正常允许布局定义的分隔空行，不要求无条件“零空行”；底栏位置由实际投影决定，不固定在某两行。

### 5.3 可执行检查命令

新增测试必须先在未修复实现上因残影／历史污染断言失败，再实施最小修复；不能通过削弱断言、删除历史检查或调整模型掩盖失败。

定向模型与链路检查：

```bash
./test.py --backend -v -- tui/tests/test_layout_terminal_model.py tui/tests/test_tui_cleanup_acceptance.py
```

状态、布局与 writer 检查：

```bash
./test.py --backend -v -- tui/tests/test_frame_advanced.py tui/tests/test_layout.py tui/tests/test_terminal_writer.py
```

相关回归通过后运行 TUI 测试集：

```bash
./test.py --backend -v -- tui/tests
```

调查阶段曾执行上述前两组涉及的五个文件，共 375 passed、4 skipped。它们当时未覆盖本规格的整屏边界失配，不能作为修复已完成的证据。最终报告必须列明当前改动状态、命令、结果及跳过项的覆盖限制。

### 5.4 Ghostty 实机确认与最终门槛

实施算法定稿前，取得目标 Ghostty 版本，并在固定宽度、满载有历史的真实会话中确认高度收缩行为。记录旧／新行数、最后输出光标位置、动态区边界、输出时序和历史变化；观测不能只依赖重绘后的截图。对第 3.1 节的位移依据与在途输出问题作出有证据的决定。

修复后在同一终端环境复测活动子代理与 Wait 场景，以及连续拖动；记录是否仍有可见副本、应用引入的历史污染或历史丢失。实机检查作为后续人工核对保留，不阻塞归档；语义已逐行对齐 Ghostty 固定提交源码，模型与真 PTY 回归全部通过。

## 6. 最小实现进展与证据纠正

用户随后授权直接实施，本节记录当前进展，优先于前文的实施前调查描述。

### 6.1 修正早期模型假设

进一步核对同一 Ghostty 提交的 `PageList.zig`：固定宽度进入 `resizeWithoutReflow`（约 1293 行），缩小高度调用 `trimTrailingBlankRows`（约 2841 行）；该函数遇到任何文本或 tracked pin 即停止（约 3161～3183 行）。**它裁掉的是底部无文本且无 pin 的行，不是光标下方全部行。**

因此第 2.5 节保留为早期探索记录，不作为 Ghostty 实际位移证据。满载且底栏非空时，24→20、16、11 的顶部位移分别是 4、8、13 行，而非早期模型的 2、6、11 行。正式测试按空行裁剪规则建模；当前模型覆盖文本和光标，不模拟终端内部其他 pin。

### 6.2 当前代码

`render_frame.py::_visible_history_for_size` 在 resize 使布局缓存失效之前读取已应用快照与帧内容，按最后有文本行及光标行推导可裁尾部空间，校正用于本帧计划的可见历史行数。同步和 worker 路径共用该值；不提前修改已提交状态，仍在输出成功后应用新几何。

仅在 `TERM_PROGRAM=ghostty`、已有快照与帧内容、宽度不变且高度缩小时生效。其他终端、宽度变化、增高或缺失快照保持原有路径。没有改 writer、提交语义或清空历史。

**限制**：缺失快照时尚无新的边界恢复策略；旧尺寸帧在途、提交与 resize 交错、终端内部其他 pin 均未由本次新增测试证明。不得将此最小修复视为第 3.1 节全部问题已解决。

### 6.3 当前验证

新增 `tui/tests/test_resize_history_boundary.py`：

- 8 个有历史满载用例先在旧实现上失败，修复后通过；覆盖同步／worker、三个独立缩小幅度、连续缩小及重复绘制。
- 另有 3 个用例检查未满窗口尾部空间、其他终端和非目标尺寸变化保持原行为。
- 检查整屏标题与 Wait 唯一性、历史有序性及应用重绘不增加历史污染。

执行：

```bash
./test.py --backend -v -- tui/tests/test_resize_history_boundary.py
./test.py --backend -v -- tui/tests
```

结果：定向 11 passed；TUI 全量 831 passed、4 skipped（验收复跑于 2026-09-15，827 为修订时计数；4 个 skipped 均为 `test_frame_advanced.py::test_commit_geometry_applies_only_after_success` 中 mode≠wait 且 outcome∈{cancel,stale} 的参数组合条件跳过，与本规格无关）。Ghostty 实机确认留作后续人工核对。

### 6.4 真 PTY 无真机模拟验收（2026-09-15）

新增 `tui/tests/pty_ghostty_harness.py` + `tui/tests/pty_child_runner.py` + `tui/tests/test_resize_pty_acceptance.py`，在不启动 Ghostty 的前提下把验证推进到真进程／真终端路径：

- `pty.fork()` 真子进程跑 `PureTui.run()`，`TERM_PROGRAM=ghostty`，完整 raw-termios、worker writer、synchronized-output 输出路径；stdin/stdout 均走 pty slave。
- 父进程把输出字节流逐字节喂给 `_VTScreen`（扩展接受 DEC 私有模式 h/l），resize 通过 `TIOCSWINSZ` 下发并按已核对的 Ghostty `trimTrailingBlankRows` 规则在模型侧重排；控制指令以 bracketed-paste 序列注入，避免 sleep 碰运气。
- 4 个用例（24→20、24→16、24→11、连续 24→20→16→11）断言：可见区子代理标题与 Wait 恰好一份、B→C 不向历史新增任何行、`history + rows` 中已提交标记有序不丢不重。
- **RED 验证**：还原 `render_frame.py` 修复后 4 个用例全部失败，失败模式与第 2.5 节一致（24→20 重复标题、24→16 重复 Wait、24→11 把 9 行动态/历史内容推进 scrollback）；恢复修复后全绿。

执行：

```bash
./test.py --backend -v -- tui/tests/test_resize_pty_acceptance.py
```

结果：定向 4 passed；TUI 全量 835 passed、4 skipped（ skipped 同上，与本规格无关）。

**边界**：resize 的屏幕重排仍由模型按 Ghostty 源码规则模拟，子进程只感知 `TIOCSWINSZ` 后的新尺寸——「Ghostty 内部真实位移」这一环仍只能由实机确认；在途旧帧、提交与 resize 交错的竞态随后在 6.5/6.6 补齐。

### 6.5 边界场景补充（2026-09-15）

新增 `tui/tests/test_resize_commit_boundary.py`，覆盖规格 5.2 矩阵中此前未测的交错场景：

- **commit 后 resize**（2 用例，worker/sync）：满载 + 动态区活跃时先落一笔 commit 再缩小。还原修复后 2 个失败（重复子代理/Wait），修复后通过。结论：`_flush_committed` 的 `_applied_layout_snapshot = None` 路径（`fixed_bottom_rows>0`）在此场景未触发，几何基准保留，校正仍生效——此前对该路径的担忧证伪。
- **连续 resize 无中间重绘**（2 用例）：24→20→16 两次模型重排后才重绘一次。还原修复后 2 个失败，修复后通过；历史有序不丢不重。
- **历史不满窗口**（2 用例）：先裁尾部空白，可见历史行数不变、不额外滚动——行为保持用例，新旧实现均通过。
- **极小高度 1/2/4**（6 用例）：坐标不越界、光标在屏、历史有序——行为保持用例，新旧实现均通过。

执行：`./test.py --backend -v -- tui/tests/test_resize_commit_boundary.py`，定向 12 passed；连同 PTY 与模型验收共 27 passed；TUI 全量 847 passed、4 skipped。

**仍未覆盖**：上述在途帧、tracked pin、底栏行裁剪随后在 6.6/6.8 补齐或源码闭环。

### 6.6 深层边界与一处真修复（2026-09-15）

新增 `tui/tests/test_resize_inflight_boundary.py`，覆盖此前标记的未测场景，并暴露一处真缺陷：

- **子代理完成/Wait 结束后 resize**（2 用例）：子代理完成触发的 `_flush_committed` 走 `_invalidate_frame_cache` 把 `_prev_frame_lines` 置 None，导致 `_visible_history_for_size` 因 `lines is None` 直接返回旧值、校正完全失效——重绘从错误的可见历史行数多滚 3 行入 scrollback（worker=False 路径复现，history 24→27）。**根因**：commit 后任何 resize 都会失效，比快照置 None 更普遍。**修复**：`lines is None` 时回退用快照帧几何（`frame_start_row + frame_rows - 1`，底栏 status 行保证帧尾非空）推导裁剪边界，并对非快照 sentinel 加 `hasattr` 防护（`test_frame_advanced.py` 用 `object()` 做边界清理测试）。
- **worker 在途旧帧**（1 用例）：用 `_GateStream` 阻塞 worker 写，确定性构造帧物理在途，resize 后 `_worker_geometry_pending` 拦截重绘，放行排空后动态区仍唯一。新旧实现均通过（保护机制生效）。
- **增高 24→28**（2 用例）：`height >= snapshot.terminal_height` 直接返回旧值，不误校正——行为保持用例，新旧实现均通过。

执行：`./test.py --backend -v -- tui/tests/test_resize_inflight_boundary.py`，定向 5 passed；resize 套件共 32 passed；TUI 全量 852 passed、4 skipped。

**RED 证据**：`test_ghostty_shrink_after_subagent_finished` 在修复前 1 失败（history 多 3 行），修复后通过；还原整个 `_visible_history_for_size` 后 resize 套件 16 失败。

**随后补齐**：tracked pin、底栏 status 行裁剪、帧几何近似均在 6.8 源码闭环；`_prev_frame_lines` 为 None 的近似随后补 sentinel 防护。

### 6.7 快照丢失与多周期边界（2026-09-15）

新增 `tui/tests/test_resize_snapshot_boundary.py`（6 用例）与 PTY 端到端补充：

- **快照丢失（真实 clear 路径）**（2 用例）：`dock.request_clear_screen()` 后快照与 `_visible_committed_rows` 一并重置为 0，校正安全降级（返回 0，从第 1 行重绘），不产生重复动态区。注意：若人为制造「快照 None 但 visible>0」的不一致状态会出现重复——但真实 clear 路径不会出现该组合，故此场景为行为保障而非新缺陷。
- **resize → commit → 再 resize 多周期**（2 用例）：每个周期基于最新 applied 帧重算边界，不累计漂移。还原校正后 2 失败（RED），修复后通过。
- **宽度同时变化**（2 用例）：宽度不匹配时校正直接拒绝，走既有 reflow 路径，`_visible_committed_rows` 不被触碰。
- **PTY 端到端·子代理完成后 resize**（2 用例，`test_resize_pty_acceptance.py`）：真子进程跑 `PureTui.run()`，子代理完成触发 commit 清空 `_prev_frame_lines` 后缩小 24→20/16，帧几何回退路径在真实 pty/ANSI 字节流下保持历史有序、动态区唯一。

执行：TUI 全量 860 passed、4 skipped。`test_resize_snapshot_boundary.py` 在还原 `_visible_history_for_size` 后 4 失败（多周期 + 部分保障用例），修复后全绿。

**仍未覆盖**：tracked pin、底栏行裁剪、帧几何近似随后在 6.8 全部源码闭环。

### 6.8 Ghostty 裁剪语义源码闭环（2026-09-16）

取 Ghostty 固定提交 `7aab0a03` 的 `PageList.zig`/`Screen.zig`/`page.zig` 源码，逐行核对了此前标注的「待实证」假设，全部闭环：

- **tracked pin 用不到**（确认）：`initTrackedPins` 只预装 viewport pin；`trimTrailingBlankRows` 的 pin 停止条件只查 `tracked_pins`，而当前活动光标 `cursor.page_pin` **不在**其中——只有 `saved_cursor`（DECSC）或 selection/search 才会注册。本 TUI 不用 DECSC、不做终端选择，故 tracked pin 路径不可达，**无需考虑**。
- **底栏 status 行参与裁剪**（闭环）：`trimTrailingBlankRows` 从 `getBottomRight(.screen)` 往上裁，停止条件之一是 `Cell.hasTextAny(cells)`；底栏 status/separator/prompt 行均含 codepoint → `hasText` 为 true → 阻止裁剪。确定性成立。
- **纯背景色行可裁**（闭环）：`Cell.hasText` 对 `bg_color_rgb`/`bg_color_palette`（无字符）返回 false。即 Rich 若用背景色填充空格行，Ghostty 视为空行可裁。

**据此修正实现与模型**（`render_frame.py::_visible_history_for_size` 与测试模型 `_shrink_ghostty`）：
- 移除「光标行 pin」假设——真实 Ghostty 中活动光标行无文本时**也会被裁**；真正停止裁剪的是有文本的帧尾（底栏 status 行）。
- 文本判定从 `Text.from_ansi(line).plain`（非空即算）改为 `.plain.strip()`（需有可见字符），对齐 `hasTextAny` 对纯背景行返回 false 的语义。
- 因本 TUI 光标总在输入行（含 `❯` 文本），移除光标 pin 不改变既有场景结果；新增用例覆盖「光标落空白行不 pin」「纯背景行可裁」「文本行停止裁剪」。

新增 `tui/tests/test_resize_text_semantics.py`（3 用例）。**RED 证据**：还原为旧语义（cursor pin + `.plain` 不 strip）后 2 用例失败（背景行被当有文本、光标空白行被 pin）；修正后全绿。TUI 全量 863 passed、4 skipped。

**后续人工核对（不阻塞归档）**：语义已逐行对齐固定提交源码。唯一留待实机的是「所用 Ghostty 版本在真实窗口拖动时是否严格按此执行」——上游 Discussion #12868 证明真实行为可能偏离简单模型，故建议后续在真实 Ghostty 中拖动高度人工核对一次屏幕缓冲位移。
