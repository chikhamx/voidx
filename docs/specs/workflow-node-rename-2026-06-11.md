# 工作流节点重命名

> **Status: In Progress**

## 背景

当前工作流节点名称过长，在 prompt context、TUI 显示、日志中占用空间大，可读性差。最长的 `verification-before-completion` 达 30 字符。

## 命名方案

| 当前名称 | 新名称 | 说明 |
|---|---|---|
| `brainstorming` | `brainstorm` | 去 -ing，更简洁 |
| `writing-design-docs` | `design-doc` | 去掉 writing- 和 -s，核心语义不变 |
| `writing-plans` | `plan` | 极简，语义明确 |
| `test-driven-development` | `tdd` | 行业通用缩写 |
| `verification-before-completion` | `verify` | 核心动作 |
| `requesting-code-review` | `review` | 核心动作 |
| `receiving-code-review` | `review-feedback` | 与 review 区分，表达"收到反馈" |
| `systematic-debugging` | `debug` | 核心动作 |

**字符数对比**：平均 18 → 7.4，最长 30 → 15。

## 影响范围

### 源码

| 文件 | 改动 |
|---|---|
| `src/voidx/workflow/nodes.py` | 8 个节点 name 字段 |
| `src/voidx/workflow/dag.py` | Edge source/target + IntentEntry nodes |
| `src/voidx/workflow/policy.py` | add() 调用中的名称字符串 |
| `src/voidx/tools/advance_workflow.py` | 示例字符串 |
| `src/voidx/tools/load_doc_template.py` | 描述字符串 |
| `src/voidx/agent/agents.py` | 描述字符串 |

### 测试

| 文件 | 改动 |
|---|---|
| `tests/test_auto_advance.py` | 大量 WorkflowRunState name + assert |
| `tests/test_tools/test_basic.py` | WorkflowRunState name + assert |
| `tests/test_tui_frame_rendering.py` | 字符串引用 |

### 文档

| 文件 | 改动 |
|---|---|
| `docs/archive/workflow-skill-dag-design-2026-06-09.md` | 归档文档，可选更新 |

## 实施步骤

1. 修改 `nodes.py` 中 8 个节点的 name 字段
2. 修改 `dag.py` 中所有 Edge 和 IntentEntry
3. 修改 `policy.py` 中所有名称引用
4. 修改 `advance_workflow.py` 和 `load_doc_template.py` 中的描述字符串
5. 修改 `agents.py` 中的描述字符串
6. 修改所有测试文件
7. 运行测试验证

## 风险

- 节点名称是持久化在 session 中的 `WorkflowRunState.name`，已有 session 数据中的旧名称不会自动迁移。决定：不做兼容，旧 session 的工作流状态失效即可。
- `tdd` 作为缩写在非开发者上下文中可能不够直观，但 voidx 的用户群体是开发者，可接受。
