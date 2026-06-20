> **Status: Done**

# 移除子代理 max_steps — 实现计划

## Goal
移除子代理的固定步数硬限制，改为 50 步安全兜底 + guard 自然终止，UI 不显示步数。

## Architecture
子代理循环从 `for step in range(1, step_budget+1)` 改为 `while step < 50`，移除 `has_tool_budget` 收敛逻辑和步数显示。

## Tech Stack
- Python 3.12+, Pydantic, LangChain

## File Structure

| 文件 | 变更 |
|------|------|
| `src/voidx/agent/graph/subagent.py` | 核心循环改造，移除 step_budget/has_tool_budget/convergence |
| `src/voidx/agent/graph/core/helpers.py` | 删除 `_subagent_step_budget` |
| `src/voidx/agent/graph/core/_voidx_graph.py` | 移除 max_steps 计算/传递/事件字段 |
| `src/voidx/tools/task_tracker.py` | 移除 TaskState.max_steps/step，tracker.start 移除 max_steps 参数 |
| `src/voidx/ui/output/events/schema.py` | SubagentStepStarted 移除 step/max_steps，SubagentFinished 移除 final_step/max_steps |
| `src/voidx/ui/output/events/consumers.py` | 适配事件字段移除 |
| `src/voidx/ui/output/capture.py` | step_header 移除 max_n 参数 |
| `src/voidx/runtime/ui.py` | step_header 移除 max_steps 参数 |
| `src/voidx/agent/graph/convergence.py` | generate_fallback_summary 适配 max_steps=0 |

## Tasks

- [ ] 1. `task_tracker.py`: 移除 `TaskState.max_steps` 和 `TaskState.step` 字段，`tracker.start()` 移除 `max_steps` 参数，`format_status()` 移除步数显示
- [ ] 2. `events/schema.py`: `SubagentStepStarted` 移除 `step` 和 `max_steps`，`SubagentFinished` 移除 `final_step` 和 `max_steps`
- [ ] 3. `runtime/ui.py`: `step_header` 方法移除 `max_steps` 参数
- [ ] 4. `capture.py`: `step_header` 移除 `max_n` 参数，不再显示步数
- [ ] 5. `consumers.py`: 适配 `SubagentStepStarted` 和 `SubagentFinished` 字段移除
- [ ] 6. `helpers.py`: 删除 `_subagent_step_budget` 函数
- [ ] 7. `convergence.py`: `generate_fallback_summary` 中 `max_steps=0` 时跳过步数行
- [ ] 8. `subagent.py`: 核心循环改造 — 移除 `step_budget` 参数，`while step < 50`，移除 `has_tool_budget`/收敛逻辑/步数显示/步数跟踪
- [ ] 9. `_voidx_graph.py`: 移除 `max_steps` 计算/传递，移除事件中的 `max_steps`/`final_step` 字段
- [ ] 10. 运行测试验证

## Tests

```bash
# 每步验证
.venv/bin/python -m pytest tests/ -v -k "subagent or task_tracker or step"
# 最终全量
.venv/bin/python -m pytest tests/ -v
```

## Risks

- 50 步兜底是否足够极端场景 — 可后续调整
- 旧 session 数据中 max_steps 字段需兼容 — Pydantic 默认值处理即可
