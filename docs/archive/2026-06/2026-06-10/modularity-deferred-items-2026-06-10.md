# 代码模块化：待办项

> **Status: Deferred**
> **Date:** 2026-06-10
> **Source:** `docs/archive/codebase-modularity-remediation-2026-06-10.md`

模块化整改 5 个阶段已全部完成。以下 3 项在原 spec 中标记为 Deferred，暂无明确计划。

## 待办

### 1. 合并 `tools/git.py` Args 模型

当前 9 个 per-command Pydantic 模型增强了校验和代码可读性。除非工具 schema 需要显著收敛，否则不优先处理。

### 2. 拆 `agent/runtime_context.py` 顶层 helper

可以在触碰 runtime context 功能时顺手做，但单独拆分收益有限。当前 608 行，15 个顶层 helper。

### 3. 重构 `ui/output/tree.py`

tree 同时承担结构和渲染映射，耦合有实际业务原因。需要先明确新边界再动。当前 570 行。
