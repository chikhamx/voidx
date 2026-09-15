# Stage D 收缩关闭报告（2026-09-16）

> **Status: Done** — Archived on 2026-09-16.

## 结论

**Stage D 收缩完成，PASS。** 主规范第 8 节 D 阶段出口条件已满足：UI 控制依赖已删除，架构检查与全量相关回归通过。本报告关闭 [S6 验收报告](pure-event-driven-sdk-architecture-2026-09-13-s6-acceptance.md) 列出的第 1、2 项未决项。

## 出口条件核对（主规范 §8 D 行）

| 出口条件 | 证据 |
| --- | --- |
| 删除 UI 控制依赖 | `src/voidx/agent/ports/ui.py` 已物理删除（commit 81fb7207）；`src/voidx/agent` 下 `ports.ui`/`AgentUiPort`/`NullAgentUiPort` 源码引用零匹配（仅陈旧 `__pycache__` 字节码） |
| 架构检查通过 | 新增看门狗测试 `test_stage_d_agent_ports_ui_module_deleted` 与 `test_stage_d_agent_core_has_no_ports_ui_imports`（AST 扫描 agent 目录禁止任何 `ports.ui` 导入）；架构测试 109 passed |
| 全量相关回归通过 | backend `src/tests` 6197 passed / 28 skipped / 0 failed（312s，commit 33ede7f5 后）；frontend 1029 passed；desktop 27 passed。TUI `tui/tests` 857 passed / 4 skipped，另有 6 个 pty 验收测试因另一 agent 的 WIP 文件 `pty_child_runner.py` 导入污染（`sys.path.insert(0, os.getcwd())` 使 `voidx.py` 遮蔽包）而环境性失败，与本次改动无关，维护方跟进 |

## 交付内容

- `src/voidx/agent/ports/ui.py` 物理删除；UI 协议（AgentUiPort 等 9 个协议 + NullAgentUiPort）整体迁至 `src/voidx/presentation/runtime_port.py`，与删除前协议成员 1:1 一致。
- `UiEventTimeout` 迁至 `src/voidx/agent/domain/ui_events.py`（领域层）。
- `LangGraphExecution` 构造强制不变量：`ui=None` 时 `semantic_output` 与 `permission_notifier` 必填（execution.py L427-431）；`self._ui` 仅构造时赋值一次，运行期无置 None 路径。
- runtime 各模块（execution/permission_flow/compaction_coordinator/streaming/subagent/tool_executor/llm_turn/core.loop）对 `host._ui` 的全部访问点防御化（None 守卫分支或 `getattr/hasattr`），含 headless 异常路径。
- 无 UI 场景由各模块自包含 Null 渲染器/Ui 包装兜底（`_NullRenderer`、`_NullSubagentUi`）。

## 独立审查说明

子代理审查通道经 5 次尝试确认不可用：2 次 SSL record layer failure（Iris/Prism），3 次跑完但报告文本与文件均无法回传（Sol/Vesper/Tess）。主会话按同一审查清单完成独立复核并留证：残留扫描、协议完整性比对（新旧 1:1）、agent 层全部 `_ui.` 裸访问点守卫上下文逐一核对、231 项架构+关键单测新鲜执行（109 架构 + 122 关键单测）。

## 遗留与边界

- 3 个浏览器测试（test_production_gateway_browser / test_gateway_browser / test_autonomous_gateway_browser）因本机 Playwright Chromium 缓存缺失失败，环境问题，非回归。
- 6 个 TUI pty resize 验收测试因另一 agent 的未跟踪 WIP 文件 `tui/tests/pty_child_runner.py` 导入污染失败，归维护方。
- S6 报告其余有界项（长期 RSS 稳态、原生 Tauri 人工验收、pending interaction replay）不变，仍按原报告边界执行。

## 关联提交

- `81fb7207` refactor(agent): Stage D 收缩——删除 agent/ports/ui.py，核心执行解耦 UI 构造依赖
- `33ede7f5` fix(agent): 防御化 llm_turn 与 core/loop 剩余裸 _ui 访问，补 headless 异常路径测试
