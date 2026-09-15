# S6 生产迁移范围验收（2026-09-15）

## 结论

**已验证功能范围 PASS；S6 整体正式验收及 Stage D 尚未通过。不得归档主规范或删除旧实现。**

本报告面向维护者，记录当前工作树实际测试状态，不以 HEAD 代表实现，不把测试通过等同于架构收缩完成。

- 当前 `src/tests` 全量、frontend 和 desktop 测试通过。
- 生产 Gateway 的 SDK 提交、指导、多轮、恢复、取消、资源收尾及真实浏览器操作已有自动化证据。
- 核心 UI 控制依赖尚未删除；独立边界审查报告缺失；完整 backend（含 TUI）未在本轮运行。这些不能被局部通过替代。

规范：[主规范](pure-event-driven-sdk-architecture-2026-09-13.md)、[自治 session 合同](pure-event-driven-sdk-architecture-2026-09-13-autonomous-session.md)。既往证据见 [S3/S4](pure-event-driven-sdk-architecture-2026-09-13-s34-acceptance.md)、[S5](pure-event-driven-sdk-architecture-2026-09-13-s5-acceptance.md)。

## 最终执行证据

工作目录为仓库根，macOS arm64；所有命令使用：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.voidx/browser-cache"
export PYTHONSAFEPATH=1
export PYTHONPATH="$PWD/src"
./python.py test.py --backend -- src/tests --junitxml="$EVIDENCE/src.xml"
./python.py test.py --frontend
./python.py test.py --desktop
```

`EVIDENCE=/tmp/voidx-s6-final-green-BEBulZ`。执行工具超时为 600 秒；实际日志分别为 `src.log`、`frontend.log`、`desktop.log`。

| 范围 | passed | failed | skipped | 嵌套 exit_code |
| --- | ---: | ---: | ---: | ---: |
| src/tests 全量 | 6193 | 0 | 28 | 0 |
| frontend 全量 | 1029 | 0 | 0 | 0 |
| desktop | 27 | 0 | 0 | 0 |

后端 JUnit：6221 tests、0 failures、0 errors、28 skipped。核对的是 `results[].status/exit_code` 与 JUnit，而非包装器外层退出码。

`before.json` 保存 src、frontend/src、frontend/css、frontend/test、desktop/tauri/src、desktop/tauri/tests 文件 SHA-256；最终核对无漂移。`git diff --check` 通过。该指纹清单不覆盖全部构建配置或环境。日志在本地 `/tmp`，尚非永久 CI 产物，不能假设长期可用。

## 已验证合同与测试入口

以下路径均位于 `src/tests/test_sdk/`，并纳入上述 src 全量：

| 合同 | 主要测试 |
| --- | --- |
| 真实 build_agent_app → facade.run(web_headless) → Gateway → SDK stream；coding/goal/loop | `test_production_gateway_submission.py` |
| 根 guidance 精确绑定 work/generation；跨根隔离、rollover、evaluator-first 恢复、预启动取消 | `test_production_gateway_guidance.py` |
| 真实 Vite/Chromium DOM 提交、审批、多轮 child、Stop、历史切换、刷新且不重新调用模型 | `test_production_gateway_browser.py` |
| 初始化错误 session 拒绝、并发取消清理、锁释放 | `test_initialization_cancellation.py` |
| 独立进程 legacy/SDK 双向真实工具写锁竞争；goal evaluator 身份与历史 | `test_goal_mixed_writer_process.py`、`test_mixed_writer_process.py` |
| 双 owner 发送失败不自等/互等；cancel/aclose 错误聚合；12 轮资源回收 | `test_production_owner_stress.py` |
| durable 重启与暂停合同 | `test_autonomous_gateway_restart.py`、`test_autonomous_gateway_needs_resume.py`、`test_autonomous_gateway_continuation.py` |

多 parent goal stop 隔离另见 `src/tests/test_goal/test_goal_stop_isolation.py`。生产浏览器操作不是通过 page.evaluate RPC 启动任务；模型回复为脚本 provider，执行、工具、服务、scheduler、evaluator 和存储使用真实实现。

## 本次收敛的主要缺陷

- 最终推送前释放根名额，旧 owner 随后删除新 generation 注册：保留名额至清理结束，并按实际 owner 身份清理。
- 最终 snapshot 仍展示 running：将终态展示与内部 admission 占用分开。
- 初始化取消先于 session 身份校验、并发取消打断清理：真实身份先校验，取消收尾受保护并等待完成。
- child 缺少真实 workspace/session 信息导致 DOM 不可发现；运行态 Stop 误走 legacy slash：使用已有 metadata 和现有 cancel 路由。
- 隐藏 transcript 的零高度首次规划省略历史，显示后不重新规划：仅该安装分支补显示后的重新规划；保留两轮历史断言。
- goal stop 未取消执行中的工具而保留写锁；全局停止 pump 又会误取消其他 parent：按当前完整 goal thread 身份取消并等待，随后持久化收尾。
- 全量发现旧装配测试要求裸引擎：更新为同时验证 LegacyWriterTurnEngine、内部 LangGraphTurnEngine 及实际注入写锁，未移除原有内部引擎类型约束。

业务 RED 与 harness/超时失败分开记录。跨进程最初的 marker 等待超时不计有效 RED；后续直接断言 stop 返回后的执行、取消 ack 和锁状态。资源压力测试为 initial GREEN，未制造 RED。

## 未完成门槛与限制

1. **Stage D 收缩未完成。** `src/voidx/agent/ports/ui.py` 仍存在；`src/voidx/agent/adapters/langgraph/execution.py` 仍有 `self._ui` 控制路径（例如权限配置、状态更新及 subagent 展示）。主规范要求所有输出迁移后删除 UI 控制依赖。现有架构测试通过不足以证明这条出口条件。
2. **独立审查缺失。** 多个审查子任务仅返回 completed，预期 `/tmp/voidx-s6-independent-boundary-review.json` 不存在，未计为 PASS。生产恢复身份的负向边界仍需正式审查。
3. **完整 backend 未运行。** 用户指定 TUI 由另一 agent 维护，本轮不修改也不运行 TUI；不因此阻塞当前 src 切片，但不能宣称全项目全绿。
4. **资源覆盖有界。** 12 轮中 6 次完成、6 次取消，12 个 bridge 可 GC，session/write 锁各重获 12 次；轻量历史可增长，未证明长期 RSS 稳态。
5. **客户端范围。** Chromium DOM 与 desktop 测试通过，不等于原生 Tauri UI 人工/端到端启动验收。
6. **恢复限制不变。** 不承诺 pending interaction replay 或 JSONL+SQLite crash-atomic；needs_resume 重启保持暂停，不自动继续模型。
7. goal stop 的初始同步 start/run_goal 并发停止及独立阻塞 evaluator 取消未被本次隔离测试单独覆盖。

因此，本报告只确认列明的测试与功能范围；最终关闭工作项仍需完成 Stage D 收缩、独立审查及适用的最终门槛。保留脏工作树和其他 agent 修改，未归档任何规范，未删除旧实现。
