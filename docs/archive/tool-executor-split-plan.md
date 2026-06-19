> **Status: Done**

# Plan: 拆分 tool_executor.py 为子包

## Goal

将 `src/voidx/agent/graph/tool_executor.py`（1259 行）拆分为 `tool_executor/` 子包，6 个模块各 < 350 行，运行时行为不变。

## Architecture

将单文件模块转为 Python 包，通过 `__init__.py` re-export 保持公开 API 导入路径不变。子模块按职责划分：types（共享类型）、guards（运行时守卫）、workflow（状态推进）、helpers（辅助函数）、ui（通知逻辑）、executor（编排入口）。

## File Structure

```
src/voidx/agent/graph/tool_executor/   # 新建子包
├── __init__.py                         # re-export 公开 API
├── types.py                            # _ExecutedTool, ToolResultOk, 常量
├── guards.py                           # 9 个守卫函数
├── workflow.py                         # 11 个 workflow 函数
├── helpers.py                          # 17 个辅助函数 + 2 个提取闭包
├── ui.py                               # 5 个 UI 通知函数
└── executor.py                         # GraphToolExecutor 类 + execute_tools
```

删除：`src/voidx/agent/graph/tool_executor.py`

## Tasks

- [ ] Task 1: 创建 `tool_executor/` 包骨架 — `__init__.py` + `types.py`
- [ ] Task 2: 创建 `guards.py`，迁移 9 个守卫函数
- [ ] Task 3: 创建 `helpers.py`，迁移 17 个纯辅助函数
- [ ] Task 4: 创建 `workflow.py`，迁移 11 个 workflow 函数
- [ ] Task 5: 创建 `ui.py`，提取 5 个 UI 通知函数
- [ ] Task 6: 创建 `executor.py`，迁移 `GraphToolExecutor` + 提取闭包
- [ ] Task 7: 更新测试文件导入路径
- [ ] Task 8: 全量测试验证

## Test Commands

```bash
# 每步后运行
.venv/bin/python -m pytest tests/ -x --timeout=30

# 最终验证
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -c "from voidx.agent.graph.tool_executor import GraphToolExecutor; print('OK')"
.venv/bin/python -c "from voidx.agent.graph.tool_executor import _ExecutedTool, ToolResultOk, AGENT_RESULT_PREVIEW_CHARS; print('OK')"
```

## Risks

- `tool_executor` 从文件模块变为包，`__file__` 值改变 — 已确认无代码依赖 `__file__`
- 测试中 22 处内部符号导入需更新路径
- `tool_execution.py` 兼容层通过 `__init__.py` re-export 保持不变
- helpers → guards 有跨模块依赖，需确保导入顺序正确
