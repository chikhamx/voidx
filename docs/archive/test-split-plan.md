> **Status: Done**

# 测试文件拆分计划 — 技术设计文档

## Context

tests/ 目录下有 19 个超过 500 行的测试文件，其中 5 个超过 1000 行。最严重的 `test_core_flow.py` 达 5000 行（占总量 15%）。过长文件导致：
- 定位测试困难，grep 结果噪音大
- PR review 时 diff 上下文丢失
- 运行单个测试场景必须加载整个文件

目标：每个测试文件控制在 **300–500 行**。

## Goals and Non-Goals

### Goals
- 将 19 个超标文件拆分为 ~45 个职责单一的文件
- 保持所有现有测试用例不变，仅做文件级重组
- 共享 fixture/helper 提取到 conftest.py 或共享模块

### Non-Goals
- 不重写测试逻辑
- 不新增测试用例
- 不改变测试目录的顶层结构（test_agent/, test_tools/ 等保持不变）

## 拆分方案

### 1. test_agent/test_core_flow.py (5000行 → 6个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_tool_result_preview.py` | ~250 | tool result preview 相关测试 (L137–250) |
| `test_graph_authorization.py` | ~700 | 权限/授权相关测试 (L252–960, L1165–1265) |
| `test_subagent.py` | ~350 | subagent runner 生命周期测试 (L982–1165) |
| `test_parallel_subagents.py` | ~600 | 并行子代理测试 (L1265–1860) |
| `test_workflow_transactions.py` | ~900 | workflow 事务/barrier/done 测试 (L2392–3120) |
| `test_core_flow.py` (保留) | ~500 | 杂项：prompt 注册、session 持久化、run_synthetic_turn 等 |

**共享 helper**：`_graph()`, `_task_state_json()`, `_edit_args()`, `_result_task_state()`, `_child_goal_resolution()`, `_child_result_contract()`, `_subagent_contract_kwargs()`, `isolated_memory_store()`, `_tree_nodes()` → 提取到 `tests/test_agent/conftest.py` 或新建 `tests/test_agent/_core_flow_helpers.py`

### 2. test_tools/test_basic.py (3239行 → 14个文件)

按现有 class 边界直接拆分，每个 class 一个文件：

| 新文件 | 行数 | 原 class |
|--------|------|---------|
| `test_tool_schemas.py` | ~145 | TestToolSchemas (L73–193) |
| `test_tool_registry.py` | ~70 | TestToolRegistry (L194–263) |
| `test_interactive_tools.py` | ~660 | TestInteractiveTools (L264–922) |
| `test_infer_state_patch.py` | ~50 | TestInferStatePatch (L923–969) |
| `test_tool_state_patch.py` | ~20 | TestToolStatePatch (L970–988) |
| `test_user_interaction.py` | ~140 | TestUserInteractionModels + TestMakeInteractCallback (L989–1147) |
| `test_load_skills_tool.py` | ~110 | TestLoadSkillsTool (L1148–1254) |
| `test_state_update_from_executed_tools.py` | ~230 | TestStateUpdateFromExecutedTools (L1255–1484) |
| `test_workflow_tool.py` | ~400 | TestWorkflowTool (L1485–1885) |
| `test_file_ops.py` | ~910 | TestFileOps (L1886–2994) |
| `test_search.py` | ~80 | TestSearch (L2995–3072) |
| `test_bash_tool.py` | ~45 | TestBash (L3073–3116) |
| `test_task_tracker.py` | ~95 | TestTaskTracker (L3117–3209) |
| `test_load_doc_template.py` | ~30 | TestLoadDocTemplate (L3210–3239) |

**注意**：`test_interactive_tools.py` (660行) 和 `test_file_ops.py` (910行) 仍超标，可在后续迭代进一步拆分。

**共享 helper**：`_replace()`, `_insert()`, `_insert_bof()` → 提取到 `tests/test_tools/conftest.py`

### 3. test_agent/test_session.py (1781行 → 3个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_session_crud.py` | ~400 | session 创建/列表/删除/标题更新 (L280–810) |
| `test_session_messages.py` | ~500 | 消息存储/加载/删除/jsonl (L98–674) |
| `test_session_transcript.py` | ~500 | transcript 节点/context frame/runtime state (L810–1781) |

**共享 helper**：`isolated_memory_store()`, `_session_dir()`, `_read_jsonl()`, `_table_names()`, `_table_columns()` → 已在 conftest 或文件内，迁移到 `tests/test_agent/conftest.py`

### 4. test_agent/test_run_loop.py (1467行 → 2个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_run_loop_startup.py` | ~500 | 启动/update check/clear/resume (L140–450) |
| `test_run_loop_turns.py` | ~500 | run_once 目标解析/workflow/title/lsp (L450–1467) |

**共享 helper**：`FakeTui`, `ExitTui`, `NoopMcpManager`, `NoopLspManager`, `_graph()`, `_disable_external_managers()` → 提取到 `tests/test_agent/_run_loop_helpers.py`

### 5. test_agent/test_stream_llm.py (1320行 → 2个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_stream_llm_sanitization.py` | ~450 | 流式清洗/DSML/replay (L151–470) |
| `test_call_llm.py` | ~500 | call_llm 集成测试 (L472–1320) |

**共享 helper**：`FakeStreamingModel` 等系列 fake class → 提取到 `tests/test_agent/_stream_llm_helpers.py`

### 6. test_ui/gateway/test_ui_events.py (1118行 → 2个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_ui_events_dock.py` | ~500 | dock 消费/streaming/status/permission (L64–352) |
| `test_ui_events_todo.py` | ~500 | todo pinned state/commit/capture console (L414–1118) |

### 7. test_skills/test_skills.py (1047行 → 2个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_skill_parsing.py` | ~400 | 文件解析/registry/discovery (L45–272) |
| `test_workflow_advance.py` | ~500 | workflow 状态推进/transition (L287–1047) |

### 8. test_agent/test_runtime_context.py (957行 → 2个文件)

| 新文件 | 行数(估) | 内容 |
|--------|---------|------|
| `test_runtime_context_builder.py` | ~650 | context 构建/incremental/skill stripping (L39–730) |
| `test_task_state_rendering.py` | ~230 | current_task_state 渲染 (L731–957) |

### 9. test_llm/test_compaction.py (892行 → 4个文件)

按现有 class 边界拆分：

| 新文件 | 行数 | 原 class |
|--------|------|---------|
| `test_token_counting.py` | ~260 | TestSelectTokenCounting (L63–322) |
| `test_fallback_summary.py` | ~90 | TestFallbackSummary (L323–412) |
| `test_compaction_retry.py` | ~420 | TestCompactionRetry (L413–831) |
| `test_overflow_threshold.py` | ~60 | TestOverflowThreshold (L832–892) |

### 10. 剩余 500–865 行文件（10个）

每个按自然边界拆为 2 个文件：

| 原文件 | 行数 | 拆分方案 |
|--------|------|---------|
| `test_tui_input_handling.py` | 865 | input dispatch / editor handling |
| `test_slash_model.py` | 862 | model switching / provider config |
| `test_tui_status_activity.py` | 809 | status rendering / activity tracking |
| `test_tui_terminal_panels.py` | 776 | panel layout / panel rendering |
| `test_tui_output_tree.py` | 750 | tree construction / tree rendering |
| `test_goal_resolver.py` | 729 | goal classification / resolver routing |
| `test_config.py` | 695 | config loading / profile merging |
| `test_llm_provider.py` | 632 | provider setup / streaming config |
| `test_lsp.py` | 559 | lsp lifecycle / tool integration |
| `test_tui_frame_rendering.py` | 555 | frame layout / frame content |

## 共享代码迁移策略

| 来源 | 目标 | 内容 |
|------|------|------|
| test_core_flow.py 顶部 helpers | `tests/test_agent/conftest.py` | `_graph`, `_task_state_json`, `_edit_args`, `_result_task_state`, `_child_goal_resolution`, `_child_result_contract`, `_subagent_contract_kwargs`, `isolated_memory_store`, `_tree_nodes` |
| test_basic.py 顶部 helpers | `tests/test_tools/conftest.py` | `_replace`, `_insert`, `_insert_bof` |
| test_run_loop.py fake classes | `tests/test_agent/_run_loop_helpers.py` | `FakeTui`, `ExitTui`, `NoopMcpManager`, `NoopLspManager`, `_graph`, `_disable_external_managers` |
| test_stream_llm.py fake classes | `tests/test_agent/_stream_llm_helpers.py` | `FakeStreamingModel`, `FakeUsageStreamingModel`, `FakeDuplicatedReasoningStreamingModel`, `FakeDsmlStreamingModel`, `FakeMalformedDsmlStreamingModel`, `TrackingStreamingModel`, `FailsOnceStreamingModel`, `FakeRenderer` |
| test_session.py helpers | `tests/test_agent/conftest.py` | `isolated_memory_store`, `_session_dir`, `_read_jsonl`, `_table_names`, `_table_columns` |

## 执行顺序

1. **Phase 1** — 最严重的 2 个文件（test_core_flow, test_basic），影响面最大
2. **Phase 2** — 1000+ 行的 3 个文件（test_session, test_run_loop, test_stream_llm）
3. **Phase 3** — 800–1100 行的 4 个文件
4. **Phase 4** — 剩余 500–750 行的 10 个文件
5. **验证** — 每拆完一个文件立即跑对应目录的测试

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 按功能模块拆分而非按行数均分 | 按行数机械切割 | 功能内聚更易维护，grep 更精准 |
| 共享 helper 提取到 conftest 或 _helpers.py | 每个文件复制一份 | DRY，但需注意 import 路径 |
| 保留原文件名给最大子集 | 全部用新名字 | 减少对 CI 和开发者习惯的冲击 |
| test_basic.py 按 class 边界拆 | 按功能主题重组 | class 边界天然清晰，拆分成本最低 |

## Open Questions

- [ ] test_interactive_tools.py (660行) 和 test_file_ops.py (910行) 是否需要二次拆分？
- [ ] conftest.py 中 fixture 命名冲突风险需在实施时逐一检查
