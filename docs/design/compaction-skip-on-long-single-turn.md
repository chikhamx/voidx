# 单轮长会话上下文超阈值但压缩被跳过的问题分析

## 状态

根因已定位，待设计修复方案。

## 问题现象

一轮会话过长（例如一个轮次内产生大量工具调用与输出）导致上下文 tokens 超过硬阈值（90% context_limit）时，压缩流程未触发：LLM 继续带着超阈值上下文运行，可能触发模型侧报错或质量下降。

## 根因

### 触发链路

压缩在所有检查点最终都汇聚到同一个选择逻辑：

1. 轮次开始前 preflight 检查（软阈值 75%）：
   `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py:317`
   → `host._preflight_compact_if_needed(...)`，reason=`"soft_threshold"`。
2. 每步 LLM 调用前硬阈值检查（90%）：
   `src/voidx/agent/infrastructure/langgraph/runtime/llm_turn.py:248`
   → `host._compaction.is_overflow({"total": loop.context_tokens})` 为真时
   `force=True, reason="hard_threshold"` 调用 `_preflight_compact_if_needed`。
3. 汇聚点：`compaction_coordinator.py:compact_for_live_state`
   → `select_preflight_details` / `select_details`（`src/voidx/llm/compaction/service.py`）。

### 失效点：轮次数不足时选择永远返回 "none"

`select_details`（`src/voidx/llm/compaction/service.py:146`）和
`select_preflight_details`（同文件 190 行）都依赖"轮次"（用户消息为起点）来划分 head/tail，
并要求至少保留一个完整旧轮次作为 tail 起点：

- `select_preflight_details`：`minimum_keep_index = max(0, len(turns) - 2)`。
  当 `turns` 只有 1–2 个时 keep_start 恒为 0 → 返回 `CompactionSelection(messages, None, 0, "none")`。
- `select_details`：`_minimum_tail_turn`（同文件 300 行）在单轮时返回唯一轮次
  （start == 0）→ `"none"`；双轮时最小保留轮次是第一轮 → head 为空 → `"none"`。

`CompactionSelection.should_compact` 要求 `mode != "none"` 且有 head 和 tail_id，
因此单轮/双轮巨型场景恒为 False。

`compact_for_live_state`（`compaction_coordinator.py:167`）在
`should_compact=False` 时直接 `return None`，即使 `over_hard=True` 或 `force=True`；
UI 显示 "Compaction skipped: no older complete turn to summarize"。

所有兜底路径（fallback_summary、`truncate_head_to_budget` 截断）都在该 return 之后，永远执行不到。
`llm_turn.py:255` 只在 result 非 None 时才更新消息列表 → LLM 继续使用超阈值上下文。

### 设计意图 vs 实际缺陷

保留完整轮次的约束（不切断单个轮次中间）是合理的对话连贯性设计；但它隐含假设
"上下文超限时至少已积累 3 个轮次"。单轮内产生海量工具输出的场景（长任务、大文件遍历、
批量 MCP 调用）不满足该假设，导致硬阈值失效。

### 复现证据

脚本构造单轮/双轮/三轮消息序列（context_limit = 128_000，硬阈值 115_200，软阈值 96_000），
验证 `CompactionService`：

| 场景 | tokens | is_overflow(90%) | select_preflight_details | select_details | 结果 |
|---|---|---|---|---|---|
| 单轮巨型（823 条消息） | 120,015 | True | none（should_compact=False） | none（should_compact=False） | 跳过 |
| 双轮（第一轮巨型 + 当前请求） | 100,165 | — | none | none | 跳过 |
| 三轮对照组（第一轮巨型） | 100,171 | — | normal，head 687 条，tail_id=u2 | normal | 正常压缩 |

- 场景 A 中 `is_overflow({"total": 120015}) == True` 且 `is_soft_overflow == True`，
  但两个选择函数均返回 `mode="none"`；
- 场景 C 表明机制本身正常，仅在轮次数 < 3 时失效。

## 修复方向（待设计确认）

- **方案 A（单轮截断）**：`should_compact=False` 且（`over_hard` 或 `force`）时，
  对巨型轮次的早期部分走现有 summary/fallback 流程，保留尾部预算
  （`preserve_recent_budget`）内的消息作为 tail。实现需在 `select_details` /
  `select_preflight_details` 中新增"单轮截断"分支。
- **方案 B（预算裁剪兜底）**：在 `compact_for_live_state` 的跳过分支前，
  对 head 候选直接按预算截断（复用 `truncate_head_to_budget` 思路），
  不需要 LLM summary，仅裁剪最旧的 tool 输出。
- **方案 C（放宽轮次下限）**：双轮时允许压缩第一轮头部（保留第一轮尾部 + 当前轮），
  单轮仍无解，需配合 A/B。

设计时需要决策：是否允许切断单个轮次中间（影响连贯性）、保留尾部预算大小、
裁剪与 summary 的优先级。核心约束是修复后任何超过硬阈值的场景都必须有兜底动作，
而不是静默继续。

## 涉及文件

| 文件 | 角色 |
|---|---|
| `src/voidx/llm/compaction/service.py` | 选择逻辑（根因所在） |
| `src/voidx/agent/infrastructure/langgraph/runtime/compaction_coordinator.py` | 跳过分支与兜底顺序 |
| `src/voidx/agent/infrastructure/langgraph/runtime/llm_turn.py` | 硬阈值触发点 |
| `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py` | preflight 触发点 |
