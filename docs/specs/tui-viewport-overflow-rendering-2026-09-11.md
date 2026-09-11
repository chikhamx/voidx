# TUI 视口溢出与终端残影修复技术规格

> **状态**：自动化验收及独立代码审查 PASS；真实终端重放待完成（不归档）
> **日期**：2026-09-11
> **适用范围**：`tui/voidx_cli/` 的视口投影、同步/Worker 绘制与滚屏；Output/Dock 生命周期仅作契约核验
> **受众**：Human + LLM
> **关联规格**：[提交瞬态清理与帧对齐](tui-commit-transient-cleanup-2026-09-11.md)

## 1. 问题与证据边界

用户截图显示同一 Subagent 的头部及旧状态多次可见，涉及 Todo 更新、工具执行和最终完成。截图是排查线索，不能单凭截图判断哪些行已提交、何时滚屏，也不能据此认定 Todo 生命周期或 `anchor_bottom` 是根因。**完整截图时序及永久残留根因尚未复现，本规格不宣称截图问题已解决。**

Todo 展开、thinking 增长、权限选择层、命令/附件面板、输入折行及终端缩小，都是需要验证的通用高度变化场景，不是已逐一复现的触发源。

## 2. 当前代码事实与待验证风险

### 2.1 帧滚屏与所有权

- `render_frame.py::_frame_scroll_plan` 仍以 `scroll_rows = min(overlap, visible)` 限制滚动量，保留只滚走可见已提交行的保护，不禁用硬件滚屏。
- `commit_output.py::scrolled_frame_payload` 已对 `set(target) | set(physical)` 全集逐行决议，包含滚动后位于新起点上方的旧活动行；滚动区域外行保持原位。这里的 physical 来自旧已应用帧，不是任意屏幕行。
- 提交路径已使用 `previous_frame_rows=0` 显式旧帧包络，未知尾部默认不清；按实际滚动映射包络，连续提交不扩大所有权。详细公式、Worker 队列与基线证明契约以关联提交规格 §1.1、§2 为单一来源。

### 2.2 同步与 Worker full/diff

`render_frame.py::_sync_layout_payload` full 与 `terminal_writer.py::_write_frame_full` 已在可信旧基线存在时清理新起点上方旧活动行，再绘制新帧；滚屏差量路径也覆盖完整旧物理集合。缺失/失效基线不能凭空推断所有权，不无条件提前 ED 起点。

物理清理与同步 `_prev_frame_lines`、Worker `_applied_lines`/起点/有效标记同步更新；定位提交只有显式包络覆盖实际基线且起点匹配才可将已清尾部标空，否则保守失效，固定 bottom 可按既有分支保留。应用状态仅在确认后结算，不消费尚未落屏 pending 几何。

### 2.3 裁切、锚定与收缩

- `tui/voidx_cli/layout.py` 的 `project_physical_viewport` 为 bottom 预留空间，再按 todo、thinking、vibe、transcript 的顺序分配剩余预算；transcript 可只保留尾部。
- 头部不在新物理投影中时，不应再画旧头部。**头部可见次数合法值可以是 0，而不是总要为 1。**
- `anchor_bottom` 是现有底端布局能力，本修复保留它。新旧起点不同不是禁用锚定的理由，应通过旧物理行决议解决残留。
- 弹窗退出、面板收起、thinking 结束会缩短帧；须验证旧帧上方及尾部行都得到处理。现有 diff 已有旧尾行比较逻辑，不能在未验证具体缺口前声称所有收缩都漏擦。

### 2.4 Todo 生命周期不是本次修改对象

`src/voidx/presentation/output/events/consumers.py` 的 `_upsert_subagent_todo_node` 当前调用 `mark_node_completed(node)`。这不等于 Todo 可以越过运行中的父节点独立提交：

- `src/voidx/presentation/output/dock/app.py` 的 `_is_node_chain_completed` 从当前节点沿 `parent` 检查祖先，直到根节点。
- 链上 `render_pending` 会阻止通过；非透明容器处于 `running` 会阻止通过，其他未完成生命周期受 `allow_untracked` 规则约束。
- 安全提交判断调用该检查。因此本修复**不将 Todo 生命周期改成 `running`，不移除完成标记**；保留透明容器等已有例外，以既有提交契约测试确认行为。

## 3. 必须保持的不变量

### 3.1 旧活动物理行全部决议

令 `P` 为真实已应用旧帧在实际滚动变换后、仍在屏幕内且仍归活动帧所有的行集合，`T` 为新物理投影：

- `P ∩ T` 按新内容覆盖或按可信基线确认相同；
- `P - T` 清理，不因位于新 `start` 上方或尾部而遗漏；
- 已提交历史和受保护固定 bottom 不属于可任意清理集合；固定 bottom 若在新投影中变化，应由正常 bottom 绘制负责。

不要用 `min(old_start, new_start)` 到屏末的无界区间代替所有权集合。允许合并连续清理范围，但必须证明中间没有历史或受保护行。

### 3.2 滚屏允许已提交历史进入 scrollback

**frame 滚屏可以把已提交历史推入 scrollback；只有未提交活动行不得随滚屏泄漏进去。** 提交资格与物理进入 scrollback 是两个阶段，不能规定只有 `plan_commit` 执行期间才能滚入历史。

输出滚屏序列前须检查滚出行的所有权；若未提交行将越界，须先清理这些物理行并在新投影中恢复仍应可见的内容，或调整安全滚动计划。滚出后再清屏无法撤回 history。正常硬件滚屏能力必须保留，历史内容顺序和唯一性不得改变。

### 3.3 清屏与基线同步

清理物理行必须在同一输出/状态应用操作中更新或使相应基线失效。范围包括同步 `_prev_frame_lines`、Worker `_applied_lines`、起点及相关 layout snapshot；下一帧需要相同文本时也必须能重绘。

沿用关联规格的提交基线契约。`render_frame.py` 已有 `_invalidate_layout_snapshots_for_commit`，不能将其重复列作新增能力。旧物理包络必须在失效前保留，且只以已应用状态为准。

### 3.4 保持投影和底端布局契约

保留 `anchor_bottom`、尾部裁切与固定 bottom；不强迫滚屏帧从 `visible_after + 1` 开始，不要求被裁切的头部重新出现。处理正确性的依据是新物理投影与终端状态一致，而非强行保持头部可见或禁止起点变化。

## 4. 实施任务

| 文件/入口 | 任务 | 必须覆盖的路径 |
| --- | --- | --- |
| `tui/voidx_cli/commit_output.py`：`scrolled_frame_payload` | 决议完整旧活动行集合，包含新起点上方；输入须能区分历史、固定 bottom 和活动区域，不能仅放宽为任意 `row >= 1`。 | 滚动区域内/外、起点漂移、旧尾部 |
| `tui/voidx_cli/render_frame.py`：滚屏计划与提交帧路径 | 传递已应用旧物理包络、实际滚动范围与保护边界；滚屏前防止未提交行越界。 | 同步与 Worker 排队/确认、固定 bottom、尺寸变化 |
| `tui/voidx_cli/render_frame.py`：`_sync_layout_payload` | 同步 full 和 diff 均执行有界旧行清理，按物理结果更新基线。 | force-full、缺失 snapshot、起点变化及收缩 |
| `tui/voidx_cli/terminal_writer.py`：`_write_frame_full` 及 diff 调用链 | Worker full/diff 使用相同所有权规则；清理与 `_applied_lines` 等状态同步。 | 基线有效/失效、仅保留 bottom、连续批次 |
| `tui/voidx_cli/layout.py` | 核验投影结果及锚定契约；无独立失败证据不修改布局策略。 | 有/无头部裁切、极小高度、bottom 预算 |

参数名和数据结构在失败测试明确输入需求后确定；避免同步与 Worker 各实现一套不一致的规则。不修改 `consumers.py` 或 Dock 生命周期来掩盖渲染错误。

## 5. 验证标准与执行约束

### 5.1 测试方法

复用 `tui/tests/test_layout_terminal_model.py` 的 `_VTScreen` 及现有流适配方式，喂入真实渲染路径产生的 ANSI。每次关键帧/提交/滚动后分别断言：

- `screen.rows` 与该帧物理投影一致，旧活动行消失，固定 bottom 正确；
- `screen.history` 无未提交头部、旧状态或其他瞬态文本；
- 已提交哨兵在 rows/history 中按预期顺序保留且无重复。

不要用 FakeStdout/原始 stdout 中字符串写出次数证明“只显示一次”；diff 重绘允许重复写同一文本。模型不是实体终端，最终仍需真实终端重放，特别是滚动区域进入 scrollback 的行为。

### 5.2 批准的验收场景

1. **上方旧行决议**：旧帧 10..15，区域滚动 3 行后为 7..12，新帧从 10 开始；明确 7..9 属于旧活动帧、没有历史，断言这些行清空。另设已提交历史哨兵，确保不被扩大清理范围擦除。
2. **同步/Worker full 与 diff**：上述几何分别走 `_sync_layout_payload` full、Worker full、差量路径，覆盖 force-full 和失效基线；检查清理后相同文本能重绘。
3. **Subagent 与 Todo 展开**：真实事件消费到渲染链路；运行祖先阻止子节点提前提交。头部若在新投影内应为 1 次，若被 tail 裁切应为 0 次，未提交头部在 history 中始终为 0。
4. **收缩与底座保护**：权限选择层、命令/附件面板打开后关闭，thinking/todo 增长后收缩；验证旧上方/尾部清空、固定 bottom 不损坏。
5. **滚屏边界**：逐步增长帧并重复滚动，已提交历史可以进入 history，未提交活动行不可以；覆盖无可见已提交行、固定 bottom 和极小视口，不以禁用滚屏通过测试。
6. **锚定与裁切**：保留 `anchor_bottom=True`，分别验证起点不变/变化及 transcript 头部完全裁掉的投影。
7. **提交交错**：与关联规格联合测试 commit 清理、帧滚动及 Worker 确认，防止清理后的旧基线被复用。

### 5.3 最终自动化交付矩阵

路径均相对于 `tui/tests/`。执行命令采用关联提交规格 §4.3 的 C2（392 passed）及 C3（713 passed），新增故障专项 C1（3 passed）；均为 2026-09-11 仓库根目录 macOS arm64 实际重跑，内部 exit_code=0、无 skipped。

| 原条款 | 准确测试定位 | 结论/边界 |
| --- | --- | --- |
| 3.1、5.2-1：新起点上方旧行、历史保护 | `test_layout_terminal_model.py::test_scrolled_payload_erases_old_rows_above_new_start`、`::test_commit_respects_explicit_applied_ownership`、`::test_commit_owned_envelope_maps_through_scroll` | PASS；只处理已应用旧包络，未知历史哨兵保留 |
| 3.3、5.2-2：同步/Worker full、滚动 diff、相同内容 | `test_layout_terminal_model.py::test_sync_full_move_clears_old_physical_rows`；`test_terminal_writer.py::test_writer_start_move_clears_old_physical_rows`；`test_tui_cleanup_acceptance.py::test_block_gap_and_identical_redraw_physical_rows` | PASS；另由提交规格矩阵覆盖 Worker 清空后基线失效/重画 |
| 2.4、5.2-3：真实 Subagent/Todo/tool 链、提交资格 | `test_tui_cleanup_acceptance.py::test_consumer_subagent_todo_tool_chain_small_viewport`；`test_output_paste.py::test_safe_flush_line_count_requires_completed_ancestors` | PASS；consumer 顺序事件、头部最多一次、运行期不入 history；非网络端到端或并发代理穷举 |
| 5.2-4：choice/命令面板退出、附件、收缩及 bottom | `test_tui_cleanup_acceptance.py::test_choice_and_slash_exit_clean_screen_and_history`、`::test_multiple_attachments_narrow_multiline_cursor`、`::test_thinking_growth_shrink_and_resize_preserve_history`；`test_layout_terminal_model.py::test_terminal_model_thinking_shrink_does_not_clear_new_bottom_rows`、`::test_terminal_model_input_patch_and_shrinking_bottom_clear_old_rows` | PASS（列出场景）；附件为输入 token/折行验收，不是全部附件面板关闭、permission details/timeout 组合 |
| 3.2、5.2-5：重复滚屏、历史顺序、瞬态保护、极小视口 | `test_layout_terminal_model.py::test_terminal_model_live_thinking_and_busy_do_not_enter_scrollback`、`::test_terminal_model_commit_then_frame_growth_keeps_live_overlay_out_of_scrollback`、`::test_terminal_model_large_logical_transcript_commits_once_without_frame_scroll`、`::test_continuous_overflow_commit_then_frame_does_not_repaint_bottom`；`test_tui_cleanup_failure_acceptance.py::test_no_safe_region_defers_without_changing_screen_or_history` | PASS；无安全区明确延期，不以强行滚屏作为验收 |
| 3.4、5.2-6：锚定、起点变化、transcript tail 裁切 | `test_layout.py::test_physical_viewport_anchor_bottom_pins_bottom_to_terminal_bottom`、`::test_physical_viewport_anchor_bottom_unfilled_terminal_aligns_frame_start`、`::test_physical_viewport_keeps_region_order_and_takes_transcript_tail_last`；`test_layout_terminal_model.py::test_sync_full_move_clears_old_physical_rows` | PASS；投影与物理几何证据，裁掉的头部不强制出现 |
| 5.2-7：提交交错、pending、失败恢复 | `test_frame_advanced.py::test_worker_defers_frame_until_pending_commit_is_applied`；`test_terminal_writer.py::test_commit_clear_prefers_last_applied_frame_start_row`、`::test_frame_generation_coalesces_against_last_applied_baseline`；`test_tui_cleanup_failure_acceptance.py::test_pending_commit_write_failure_preserves_physical_state_and_retries` | PASS；提交阻挡下一帧、失败无计数结算、物理内容保护、健康 writer 重试；不保证部分输出回滚 |
| 5.1、5.3：真实终端重放与最终审查 | 尚无真实终端环境/执行结果；主 agent 尚待最终审查 | **待完成**，不删除原批准条件，不以模型通过代替 |

### 5.4 限制与交付状态

自动化矩阵 PASS，本轮未发现实现缺口。原截图不具备精确事件重放证据，不宣称重复头部/旧状态的现场根因已解决。保留 §5.1 要求的真实终端重放待办；主 agent 负责最终审查及决定归档，本轮不归档。

VT 模型不是实体终端：resize 只保留左上 cells 并裁剪/补齐，不支持 reflow、真实滚动区 scrollback 差异、复杂 grapheme 或在途 SIGWINCH。本矩阵只覆盖准确列出的有限组合；真实 reflow、并发/多层代理、任意故障与 resize/choice 排列不是自动追加到原批准范围的新阻断。真实事件链从 consumer 开始，不含网关传输；choice 覆盖 Escape 退出，图片测试 mock 系统剪贴板边界。

本轮仅增加验收测试与文档，未改实现；测试构造失败不作为实现 TDD RED。实现修复的 RED/GREEN 历程见关联规格；不能将自动化 PASS 写成全部现场验收完成。

## 6. 禁止事项

- 不引入 Alternate Screen，不以全屏刷白掩盖残留，不禁用正常硬件滚屏。
- 不按 `todo`、`bash` 等工具名特化清屏或投影。
- 不改 Todo 生命周期为运行态，不禁止 `anchor_bottom`。
- 不擦除已提交历史或受保护固定 bottom，不只修改物理输出而遗漏基线更新。

## 7. 简短实现历程

初版补全滚动后旧行集合及同步/Worker full 清理，692 项通过；闭环发现提交所有权不足，随后显式 `previous_frame_rows` 与基线证明修复至 700 项；consumer/动态布局验收至 710 项；本轮故障物理断言后 713 项通过。旧“所有权未修复”“事件链未补” BLOCKED 记录已由正式测试替代；真实终端重放与主 agent 最终审查仍待完成。

### 最终代码审查记录

2026-09-11：独立代码审查 PASS，核对提交所有权、滚动映射、已应用基线、历史保护与首写失败恢复；当前 TUI 测试 **713 passed**。代码与自动化验收已完成，真实终端重放仍是归档阻断，不以模型结果代替。部分写入后的物理回滚不在本次通过保证范围内。


### 临时面板收缩规则补充（2026-09-11）

本节取代临时面板“触底后持续贴底”的旧行为，不改变正常固定 bottom 提交规则。

- 展开临时面板先使用活动帧尾部空白；高度不足时允许正常硬件滚屏，仅滚缺少的高度，已提交历史可进入 scrollback。
- 当前逻辑 bottom 含 panel，或上一已应用快照含可见 panel 时，整帧不继承仅由触底位置推断的 bottom anchor。同步和 Worker 使用同一判断；提交仍使用物理 dock anchor，保护固定 bottom。
- 收起后从滚屏后的活动帧起点投影，输入框上移，缩短部分清为空白留在末尾。后续无内容变化的 tick/full frame 不重新压到底部；重复打开优先复用尾部空白。
- 不回拉 scrollback、不恢复旧截图、不重印已提交历史、不改变 Dock 生命周期。新输出的正式提交可以推进活动帧起点；resize 重新计算可用几何，不保证固定绝对行号。
- 新增 `test_layout_terminal_model.py` 中 `test_slash_del_keeps_scrolled_origin_and_trailing_space`（8 组合）及 `test_slash_del_with_output_resize_and_busy_tick`（4 组合）：真实 `_process_input(b'/') → _render_after_input → DEL → _render_after_input`，同步/Worker、committed/transcript、idle/busy、历史哨兵唯一性、三次开关、缺少高度滚动、后续帧、菜单期间输出/提交、65×16→80×12 resize、busy tick 及合法整帧回退。
- TDD 首轮 8 项均在关闭后的起点断言 RED（如 9 != 1）；修复后 focused 12 passed，全量 `./test.py --backend -- tui/tests` 为 **725 passed**。既有断言全部保留。详细命令、测试构造调整与边界见 `.voidx/slash-shrink-fix.md`。

自动化通过不代替真实终端验收；本规格不归档，主 agent 独立审查和真实终端重放仍待完成。


### 第二轮：commit 清快照后直接 DEL（2026-09-11）

独立 review 发现上一轮遗漏：真实 commit 清掉 LayoutSnapshot 后，不重绘菜单直接 DEL，sync/worker 都将合法起点 2 错移到 8。上一轮 725 项通过不代表该边界已修复。

- `RenderState.applied_temporary_panel` 分离已应用临时 panel 身份与几何快照；仅成功应用帧时更新。commit 保留身份但仍清除过期几何；clear/resize/terminal failure/overflow 重置身份，旧 generation/epoch 回调不得恢复状态。
- 整帧锚定使用该身份；提交仍使用原物理 bottom 保护，不更改 `fixed_bottom_rows`。worker scroll barrier 未应用时延后新帧并请求调度重绘，避免快速开关以旧 visible rows 重复滚动。
- 新增真实提交→直接 DEL（sync/worker）与快速开关共 4 组合；直接关闭立即断言提交后的合法起点、尾空白和历史唯一性，再验证 scheduled frame/busy tick。重开允许且只允许滚动缺少高度。另增 4 组已应用/pending 回调与失效边界验证。
- 第二轮 RED：校准快速重开的合法一行滚动后，3 failed / 1 passed；直接 DEL 均为 8 != 2，worker 快速开关额外滚动 2 != 1。最小身份修复后剩快速 worker 1 failed；加 scroll barrier 等待后 GREEN。
- 最终 `./test.py --backend -- tui/tests/test_layout_terminal_model.py -k slash`：20 passed；`./test.py --backend -- tui/tests`：733 passed（4.49s），均检查 suite 内层 exit_code=0。主 agent review 通过本轮实现范围，未委派；真实终端人工验收仍待进行，不归档。

详细第二轮证据及限制见 `.voidx/slash-shrink-fix.md`、`.voidx/slash-commit-fix.md`（本地记录，不提交 `.voidx/`）。
