# TUI 提交瞬态行残留清除与帧对齐技术规格

> **Status: Done** — Archived on 2026-09-12.

> **状态**：自动化验收及独立代码审查 PASS；完成归档
> **日期**：2026-09-11
> **适用范围**：`tui/voidx_cli/` 的提交规划、同步渲染与终端 Worker；Dock 提交资格仅作为现有契约核验
> **受众**：Human + LLM
> **关联规格**：[视口溢出与终端残影](tui-viewport-overflow-rendering-2026-09-11.md)

## 1. 问题与证据边界

TUI 使用主屏幕内联渲染，不使用 Alternate Screen。已提交转录与可重绘活动帧共享物理屏幕，后者包含 vibe、thinking、todo 及 bottom 等区域；区域名称本身不代表提交资格。

用户截图中，旧 `Fermenting (22s ...)` 与当前 `Fermenting (28s ...)` 同时可见。**已证实的是 commit 后、下一帧绘制前，旧瞬态行可能暂留；截图中的永久残留根因及完整时序尚未复现。** 本规格约束提交边界清理，不能据此宣称截图问题已解决。

### 1.1 当前实现事实

- `commit_output.py::plan_commit` 已接受 `previous_frame_rows=0`：默认未知旧包络不清尾部。旧 owned end 限于 `min(max(start_row, 1) + max(previous_frame_rows, 0) - 1, height - fixed_bottom_rows)`，按实际 `scrolled_rows` 映射，只清提交末尾之后仍属于旧包络的可见行；不是清至屏末。
- `app.py::_flush_committed` 传入真实已应用 `_last_frame_rows`，同步与 Worker 使用同一 planner。提交后按旧包络终点、实际滚动和新起点收缩剩余行数，避免连续提交扩大所有权；无安全区域返回 `None` 并保留输出请求。
- Worker 的内存/落盘提交队列均携带 `previous_frame_rows`。定位 preserve 分支仅在起点匹配且显式包络覆盖实际基线时将清空的剩余行标空；未知/不足包络使基线失效。经验证未变的固定 bottom 可单独保留。该参数是调用方的清理证明，不解析任意 ANSI 自动推断所有权。
- 同步 `_prev_frame_lines` / 起点与 Worker `_applied_lines` / `_baseline_valid` 必须反映实际落屏结果；排队成功不代表确认。复用 `render_frame.py::_invalidate_layout_snapshots_for_commit`，不能用 pending 几何代替 applied 几何。
- `src/voidx/presentation/output/tree.py` 的 block gap 是 `lines.append("")` 生成的真实空字符串投影行，不是物理跳行。

### 1.2 证据边界

输入折行、附件与光标已有物理模型验收；原始截图的完整时序仍不能精确重放。模型通过证明下面列出的机制和场景，不证明永久残留的现场根因已经复现或所有终端行为一致。

## 2. 必须保持的不变量

### 2.1 提交清理与历史保护

1. 只清理有所有权证据的上一帧可重绘物理行；已提交历史（包含仍在屏幕内的历史）不得被擦除或重复输出。
2. commit 批次完成时，未被本次持久内容覆盖的旧瞬态行必须已清理，不依赖下一帧碰巧覆盖。
3. 清理不得擦除本次刚提交的行，也不得触及受保护的固定 bottom；未固定的旧 bottom 若属旧帧，则与其他可重绘行一样参与决议。
4. 若提交滚屏会将未提交行送出可见屏幕，必须在滚动前处理；事后 `EL` 无法修复 scrollback 泄漏。正常滚屏可让已提交历史进入 scrollback。

### 2.2 几何必须按实际滚动变换

无滚动时，可从旧包络 `[S_prev, E_prev]` 中减去本次提交覆盖范围与固定 bottom 范围，得到尾部候选清理行。候选行还须落在屏幕内且归旧帧所有。

发生滚动时，不能直接套用 `old_end = start_row + previous_frame_rows - 1` 或 `next_row = start_row + L`：应按实际滚动区域与次数变换旧行位置，并记录新写入行的最终位置；区域外的固定 bottom 不移动。只清理变换后仍可见、未被持久输出消费的旧帧行。`CommitOutput.next_row`、`scrolled_rows` 与清理计划必须一致。

### 2.3 物理清理与基线更新是同一操作

**清空物理行的同一提交操作，必须同步更新或使相应渲染基线失效。** 禁止只改 ANSI 而保留“该物理行仍有旧文本”的 diff 假设，否则下一帧内容恰好相同时会被跳过。

- 同步路径：核对 `_prev_frame_lines`、`_prev_frame_start_row` 与 layout snapshot。
- Worker 路径：核对 `_applied_lines`、`_applied_start_row`、`_baseline_valid`、`_bottom_only_baseline` 与批次确认后的应用状态。
- 可以保留经验证未改变的固定 bottom 基线；清理过的行不得继续作为有效文本基线。
- 复用现有提交快照失效机制，不重复增加同义失效逻辑；失效前应保留清理所需的已应用物理包络，不能用尚未落屏的 pending frame 代替。

## 3. 实施任务与边界

### 3.1 提交规划：`tui/voidx_cli/commit_output.py`

- 为 `plan_commit` 提供足够的已应用旧帧几何/所有权信息，规划有界清理；参数形式由实施时按调用链确定，不预设单一 `previous_frame_rows` 足以覆盖滚动。
- 优先对明确拥有的行使用 `CUP + EL`。只有证明确认清理范围不含历史或固定 bottom 时，才可使用范围清除。
- 清理顺序必须保护滚屏前仍未提交的内容；保留当前无法安全提交时返回 `None`、等待建立保护区域的行为。

### 3.2 同步提交与应用状态：`tui/voidx_cli/app.py`

- `_flush_committed` 传递真实已应用包络，执行提交计划后按实际物理结果更新基线及提交起点。
- 保持原有确认、失败恢复与延期提交语义；Worker 排队成功不等于已经落屏。
- 检查现有 busy activity 布局失效与新清理范围是否一致，仅补缺失分支，不无条件新增重置。复用 `render_frame.py` 的提交快照失效入口。

### 3.3 Worker 与后续绘制

- `tui/voidx_cli/terminal_writer.py`：`_process_commit` 将清理结果与基线变更一并应用，兼容 positioned、preserve-baseline、固定 bottom、无固定 bottom 分支。
- `tui/voidx_cli/render_frame.py`：核验后续同步 diff/full（包括 `_sync_layout_payload`）消费的是清理后基线。
- 与关联规格共用物理所有权和基线规则；不要在规划器、同步路径与 Worker 中各自维护一套不同的清理算法。

## 4. 验证与完成条件

### 4.1 先写失败测试，再实施代码

复用 `tui/tests/test_layout_terminal_model.py` 中真实存在的 `_VTScreen` 及现有流适配方式。将实际提交和绘制 ANSI 喂入模型，检查 `screen.rows` 与 `screen.history`；原始 stdout 的重复写入次数不是可见残留证据。ANSI 指令断言仅作辅助。

批准的验收场景（对应测试见最终矩阵）：

1. **提交间隙清理**：高度 16，旧转录在 10、旧 vibe 在 11、固定 bottom 在 12..16；提交一行后、下一帧前，11 行为空，提交行及 bottom 完整，history 无旧 vibe。
2. **清理后相同内容重绘**：下一帧仍需要被清空行的同一文本；同步与 Worker 的保留基线/失效路径都必须真正重绘该行，不能因 diff 相等漏画。
3. **block gap 回归**：提交后下一帧包含真实空字符串 gap；按新投影逐行验证屏幕，旧时间戳在 rows/history 中为零，当前 vibe 仅在投影包含它时出现一次。
4. **滚动与历史**：覆盖无滚动、受保护区域滚动、多行提交、无安全保护区延期；已提交哨兵文本在 rows/history 中保持顺序且不重复，瞬态行不进入 history，固定 bottom 不变。
5. **输入与收缩**：多行附件、窄宽折行、旧 thinking/todo/bottom 收缩；验证光标、尾部空白及持久内容不受损。
6. **确认时序**：Worker 提交与下一帧排队、确认及失败恢复；不使用 pending 几何清理已应用帧。

### 4.2 最终自动化交付矩阵

下表路径均相对于 `tui/tests/`；同格多个测试共同覆盖条款，不表示一个用例覆盖全部组合。命令 C1/C2/C3 见下节。

| 原条款 | 准确测试定位 | 结论/边界 |
| --- | --- | --- |
| 2.1、4.1-1：间隙清理、固定 bottom、未知历史保护 | `test_layout_terminal_model.py::test_commit_clears_live_tail_before_next_frame`、`::test_commit_respects_explicit_applied_ownership`、`::test_continuous_commits_do_not_expand_old_envelope` | PASS；显式零/有限/超界包络与连续提交 |
| 2.2、4.1-4：滚动映射、多行、历史唯一 | `test_layout_terminal_model.py::test_commit_owned_envelope_maps_through_scroll`、`::test_terminal_model_multiline_commit_preserves_exact_baseline`、`::test_terminal_model_commit_at_viewport_edge_has_no_trailing_scroll`、`::test_terminal_model_large_logical_transcript_commits_once_without_frame_scroll` | PASS；真实 ANSI→rows/history |
| 2.3、4.1-2：清理与基线、相同内容重绘 | `test_terminal_writer.py::test_positioned_commit_cleared_baseline_repaints_identical_remaining_lines`；`test_layout_terminal_model.py::test_positioned_worker_does_not_assume_unknown_tail_is_blank`；`test_tui_cleanup_acceptance.py::test_block_gap_and_identical_redraw_physical_rows` | PASS；应用级 sync/worker + Writer 保守失效 |
| 4.1-3：真实 gap、新投影、旧瞬态消失 | `test_tui_cleanup_acceptance.py::test_block_gap_and_identical_redraw_physical_rows`、`::test_consumer_subagent_todo_tool_chain_small_viewport` | PASS；逐行投影及旧状态，不是原截图时间戳精确重放 |
| 4.1-4：无安全区延期 | `test_frame_rendering.py::test_commit_without_safe_region_is_deferred_without_settling`；`test_tui_cleanup_failure_acceptance.py::test_no_safe_region_defers_without_changing_screen_or_history` | PASS；sync/worker 两次延期，非空 history、bottom、cursor 不变，计数与 force 请求保留 |
| 4.1-5：附件、输入、thinking/todo/bottom 收缩 | `test_tui_cleanup_acceptance.py::test_multiple_attachments_narrow_multiline_cursor`、`::test_thinking_growth_shrink_and_resize_preserve_history`、`::test_consumer_subagent_todo_tool_chain_small_viewport`；`test_layout_terminal_model.py::test_terminal_model_input_patch_and_shrinking_bottom_clear_old_rows` | PASS；有限尺寸/事件序列 |
| 3.2、4.1-6：确认/pending 几何、失败恢复 | `test_frame_advanced.py::test_worker_defers_frame_until_pending_commit_is_applied`；`test_terminal_writer.py::test_commit_clear_prefers_last_applied_frame_start_row`、`::test_frame_generation_coalesces_against_last_applied_baseline`、`::test_worker_failure_invalidates_baseline_and_recovers_terminal_state`；`test_tui_cleanup_failure_acceptance.py::test_pending_commit_write_failure_preserves_physical_state_and_retries` | PASS；真实 Worker 首次 write 前故障，pending 阻挡下一帧，rows/history 不变，失效并清 pending；替换健康 writer 后重试一次。不是部分输出回滚保证 |

### 4.3 本轮命令与结果（2026-09-11）

仓库根目录、macOS arm64、项目 Python 环境；先 focused 再全套，按 runner 内部 exit_code 判定：

```bash
# C1：新增故障边界，3 passed
./test.py --backend -- tui/tests/test_tui_cleanup_failure_acceptance.py
# C2：两份规格相关集合，392 passed
./test.py --backend -- tui/tests/test_tui_cleanup_failure_acceptance.py tui/tests/test_tui_cleanup_acceptance.py tui/tests/test_layout_terminal_model.py tui/tests/test_terminal_writer.py tui/tests/test_layout.py tui/tests/test_frame_rendering.py tui/tests/test_frame_advanced.py tui/tests/test_output_stream.py tui/tests/test_output_tree.py tui/tests/test_output_paste.py
# C3：TUI 全套，713 passed
./test.py --backend -- tui/tests
```

本轮仅补验收测试/文档，无实现修改，不将测试构造错误称为实现 RED。此前所有权修复曾验证 7+1 项 RED，最终 700 项 GREEN；事件链补充后 710 项 GREEN；本轮 713 项是重新执行结果。

### 4.4 剩余完成条件与限制

- **自动化矩阵 PASS，无本轮确认的实现缺口；最终交付仍待主 agent 审查。** 原批准规格的真实终端重放尚无环境/执行结果证据，保留为待完成验收，不删条款，也不宣称已解决截图。
- VT 模型只支持既有 ANSI 子集；resize 保留左上 cells 并裁剪/补齐，不支持真实终端 reflow、复杂 emoji grapheme、在途 SIGWINCH。真实 reflow、并发/多层代理及任意 resize/choice/故障交错不自动升级为本规格新增阻断或无限组合要求。
- 事件链覆盖 consumer→Dock→TUI→Writer→VT，不含网络/bus；子代理工具 progress 按现有 consumer 契约是瞬态，不应被误判为丢失持久工具历史。choice 验证 Escape 退出，附件 mock 系统剪贴板边界。
- 首次 write 前失败保护不等于已部分输出后的终端回滚，也不证明故障 writer 能继续接受提交；重试用健康 writer。真实终端重放及主 agent 最终审查通过前不归档。

## 5. 禁止事项

- 不引入 Alternate Screen，不用全屏清空掩盖残留。
- 不按工具名称特化，不修改 Dock 节点生命周期来绕开渲染问题。
- 不擦除已提交历史或固定 bottom，不只清物理行而遗漏基线更新。
- 不把 block gap 当物理跳行，不把提交暂留等同于已确认的永久历史泄漏。

## 6. 简短实现历程

初版 692 项回归通过，但闭环探针暴露未知尾部误清、缺少显式所有权。随后增加 `previous_frame_rows` 与 Worker 保守基线契约，正式测试取代旧外部探针，700 项通过；真实 consumer/动态布局补充至 710 项；本轮补故障物理验收至 713 项。旧“未增加包络”“所有权未修复”“事件链待补”等 BLOCKED 记录已失效，不代表当前状态。现场截图重放与最终审查仍待完成。

### 最终代码审查记录

2026-09-11：独立代码审查 PASS，核对提交所有权、滚动映射、已应用基线、历史保护与首写失败恢复；当前 TUI 测试 **713 passed**。代码与自动化验收已完成，真实终端重放仍是归档阻断，不以模型结果代替。部分写入后的物理回滚不在本次通过保证范围内。
