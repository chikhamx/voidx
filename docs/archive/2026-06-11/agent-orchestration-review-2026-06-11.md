# voidx Agent 编排、提示词与工作流审查报告

> **审查日期**: 2026-06-11
> **审查范围**: agent 编排、提示词系统、workflow runtime、权限系统、子 agent 系统
> **审查方法**: 源码逐文件阅读 + 逻辑推演 + 闭环验证

---

## 一、系统架构概览

### 1.1 Agent 图/编排

LangGraph 状态机 (`topology.py`)，4 节点循环图：

```
prepare → call_llm → [execute_tools → call_llm]* → finalize → END
```

5 个 Agent 定义 (`agents.py:345-446`)：

| Agent | 写权限 | 委派 | max_steps | 工具数 |
|-------|--------|------|-----------|--------|
| orchestrator | ✅ | ✅ | 100 | 22 |
| explore | ❌ | ❌ | 25 | 11 |
| plan | ❌ | ❌ | 30 | 11 |
| implement | ✅ | ❌ | 100 | 14 |
| review | ❌ | ❌ | 30 | 11 |

子 Agent 执行 (`subagent.py`)：独立 for-loop，不走 LangGraph 图，深度限制 = 1。

### 1.2 提示词系统

`RuntimeContextBuilder` 组装流程：

1. Base System Prompt（通信风格、全局规则、Workflow Runtime 说明）
2. Role Prompt（ORCHESTRATOR / EXPLORE / PLAN / IMPLEMENT / REVIEW）
3. Mode Prompt（PLAN_MODE_APPEND）
4. Tool Contract（从 AgentDef 动态生成）
5. Instructions（AGENTS.md / CLAUDE.md 层级加载）
6. Skill Context / Workflow Context（活跃节点完整展开，非活跃节点摘要）
7. Runtime Envelope（日期、workspace、model 等）

最终消息结构：

```
[SystemMessage: system_content]
[HumanMessage: skill_context_content]   ← workflow context
[...semantic_messages...]               ← 历史 + 当前用户消息
```

### 1.3 Workflow Runtime

DAG 定义 (`dag.py`)：8 个节点，13 条边：

```
brainstorming ──approved──→ writing-design-docs ──completed──→ writing-plans ──approved──→ tdd
brainstorming ──skip_to_plan──→ writing-plans
brainstorming ──small_change──→ tdd
tdd ──implemented──→ verification
verification ──passed_substantial──→ requesting-code-review
verification ──failed_implementation──→ tdd
verification ──failed_bug──→ systematic-debugging
systematic-debugging ──nontrivial_fix──→ tdd
systematic-debugging ──trivial_fix──→ verification
requesting-code-review ──review_has_issues──→ receiving-code-review
receiving-code-review ──feedback_valid──→ tdd
receiving-code-review ──feedback_verified──→ verification
```

激活机制：基于 intent + agent + interaction_mode + 文本关键词。
Gate 执行：工具授权时先检查 `workflow_denied_tools`，被 gate 拒绝的工具直接 deny。

---

## 二、严重问题（逻辑不自洽 / 闭环断裂）

### 🔴 P1：`agent implement ask` 权限规则是死代码

**位置**: `permission/rules.py:44,50`

```python
Rule(permission="agent", pattern="*", action="allow"),      # 第44行
Rule(permission="agent", pattern="implement", action="ask"), # 第50行
```

**问题**: `evaluate.py` 中规则按顺序匹配，先匹配者胜出。`agent(agent="implement")` 的 pattern 是 `implement`，同时匹配 `*`（allow）和 `implement`（ask），但 `*` 排在前面，所以 `agent implement ask` 规则永远不会生效。所有子 agent 委派都是 allow，包括 implement。

**影响**: 设计意图是 implement 委派需要用户确认，但实际被静默绕过。这是一个安全漏洞。

**修复建议**: 调换规则顺序，将 `agent implement ask` 放在 `agent * allow` 之前；或改用更精确的匹配逻辑（如最长匹配优先）。

---

### 🔴 P2：Intent 变更无法撤销已激活的 Workflow Gate

**位置**: `agent/graph/core.py:435-456`, `agent/intent_refinement.py`, `workflow/runtime.py`

**问题**: 执行流程：

1. `turn_runner` → `resolve_turn_intent` → 初始 intent（如 `debug`）
2. `_prepare_with_stream` → `skill_context_for` → 激活 `systematic-debugging`（gate: denied write/edit/apply_patch）
3. LLM 调用 `on_intent` → `refine_intent` → intent 改为 `implement`
4. `_merge_skill_runs` 合并，但**不移除已激活的 runs**

结果：intent 从 debug 变为 implement，但 `systematic-debugging` 的 gate 仍然拒绝写工具。Agent 被锁死在矛盾状态——intent 说要实现，gate 说不能写。

**影响**: 特定 intent 转换场景下 agent 完全卡死，无法继续工作。

**修复建议**: `refine_intent` 应计算新 intent 对应的 workflow activations，与已有 runs 对比，将不再匹配的 runs 状态设为 SKIPPED 或 SATISFIED。

---

### 🔴 P3：Workflow Context 在 Compaction 后可能丢失 Gate 信息

**位置**: `agent/runtime_context.py:140-143`, `agent/graph/compaction_coordinator.py`

**问题**: workflow context 作为独立的 `HumanMessage` 插入消息序列。compaction 会摘要/删除历史消息，如果 workflow context message 被 compaction 删掉，gate 的具体 denied tools 列表就丢失了。

system prompt 只有一句"follow workflow gate"，但具体的 denied tools 列表在 workflow context message 中。Gate 约束在长对话中可能被静默解除。

**影响**: 长对话中 workflow gate 可能失效，agent 在应该被约束的情况下执行了被 gate 拒绝的操作。

**修复建议**:
- 方案 A：将 workflow context message 标记为不可 compaction（在 compaction 时保留）
- 方案 B：将 gate denied tools 列表注入 system prompt（每次 prepare 时重建）
- 方案 C：在 compaction summary 中包含当前 active workflow gate 信息

---

### 🔴 P4：`done` Exit 无结构化定义，跨 4 个文件硬编码

**位置**:
- `workflow/render.py:30,51,89`：渲染时硬编码 `"done -> end"`
- `tools/advance_workflow.py:24`：`condition` 默认值 `"done"`
- `workflow/policy.py:107`：`workflow_exit_summaries` 硬编码追加 `"done -> end"`

**问题**: `dag.py` 的 Edge 列表中没有 `done` 边，`schema.py` 的 `_validate_references` 也不验证它。`done` 是隐式协议，无法被结构化验证，也无法表达"done 后激活特定后继"的需求。

**影响**: 如果某个节点需要 `done` 走向特定后继（而非直接结束），当前架构无法表达。`done` 的行为完全依赖各处硬编码的一致性。

**修复建议**: 在 `WorkflowDAG` 中增加 `done_edge` 的结构化定义，或引入一个 `END` 虚拟节点作为 `done` 的统一 target。

---

## 三、中等问题（设计不一致 / 冗余）

### 🟡 P5：`can_delegate` 字段是运行时死字段

**位置**: `agent/agents.py:259`, `agent/graph/subagent.py:76`

**问题**: `can_delegate` 在运行时从未被检查。`subagent.py:76` 的过滤只看 `agent_def.tools`，不看 `can_delegate`。它只用于 `tool_contract` 的文本描述，对 LLM 行为没有强制约束。

**修复建议**: 要么在 `subagent.py` 的工具过滤逻辑中检查 `can_delegate`，要么移除该字段并统一用 `tools` 列表控制。

---

### 🟡 P6：子 Agent 绕过 LangGraph 状态管理

**位置**: `agent/graph/subagent.py:178-`

**问题**: 子 agent 的工具执行直接调用 `tool.execute()`，不经过 `GraphToolExecutor`。导致：
- 无 `todo_updated_event`
- 无 compaction 机制（长任务可能 context 溢出）
- 无 `_needs_failure_check` 逻辑

**修复建议**: 将子 agent 的工具执行也接入 `GraphToolExecutor`，或在 `run_subagent` 中补充缺失的状态管理逻辑。

---

### 🟡 P7：`verification-before-completion` 无 Gate

**位置**: `workflow/nodes.py` — `VERIFICATION_BEFORE_COMPLETION`

**问题**: gate 是空的 `NodeGate()`，没有 `denied_tools` 也没有 `required_before_transition`。语义上"验证通过才能声称完成"，但没有约束力。Agent 可以在验证阶段继续实现新功能而不受约束。

**修复建议**: 添加 `required_before_transition: "verification passed or failed with evidence"`，并考虑 `denied_tools` 限制新功能的实现。

---

### 🟡 P8：子 Agent 的 `authorize_tools` 闭包传入错误的 `plan_mode`

**位置**: `agent/graph/core.py:448`

**问题**: `plan_mode=self._plan_mode`（父的 plan mode），但 plan agent 的 interaction_mode 已被设为 `PLAN`。当前 permission engine 用 `PermissionContext.interaction_mode` 做判断，所以不是运行时 bug，但 `plan_mode` 参数语义错误，未来重构可能引入 bug。

**修复建议**: 从子 agent 的 `interaction_mode` 推导 `plan_mode`，而非直接用父的值。

---

### 🟡 P9：ORCHESTRATOR_PROMPT Decision Flow 与 Workflow Gate 优先级依赖 LLM 理解

**位置**: `agent/agents.py:49,77-133`

**问题**: `BASE_SYSTEM_PROMPT:49` 只有一句"workflow gate takes precedence"，但 Decision Flow 的"fix/implement/modify → edit directly"与 workflow gate 的"denied tools = write, edit"直接矛盾。优先级完全依赖 LLM 正确理解那句声明。

**修复建议**: 在 Decision Flow 的每个涉及写操作的分支中，显式添加"unless blocked by an active workflow gate"条件。

---

### 🟡 P10：`apply_patch` 在 Permission Rules 中无显式规则

**位置**: `permission/rules.py:26-53`

**问题**: `BASIC_RULES` 有 `write`、`edit`、`lsp_format` 的 ask 规则，但没有 `apply_patch`。它通过 `capability=FILE_WRITE` 隐式走到 ask，但不如其他写工具显式。

**修复建议**: 在 `BASIC_RULES` 中添加 `Rule(permission="apply_patch", pattern="*", action="ask")`。

---

## 四、轻微问题（可改进）

### 🟢 P11：`task_status` 在子 Agent 过滤中硬编码排除

**位置**: `agent/graph/subagent.py:76`

**问题**: `allowed_ids = set(agent_def.tools) - {"agent", "task_status"}`。但当前没有任何子 agent 的 tools 列表包含 `task_status`，这个排除是空操作。

**修复建议**: 移除该硬编码，或改为从 agent 定义中推导。

---

### 🟢 P12：Plan Agent 的 PLAN_PROMPT 为空字符串

**位置**: `agent/agents.py:159`

**问题**: `PLAN_PROMPT = ""`。plan agent 完全依赖 tool_contract 和 base system prompt，没有角色特定的行为指导。

**修复建议**: 为 plan agent 添加角色提示词，至少包含输出格式要求和约束。

---

### 🟢 P13：子 Agent 结果只有文本，结构化信息丢失

**位置**: `agent/graph/subagent.py:252-267`

**问题**: 返回 `extract_text(assistant_msg)` 字符串。文件变更列表、todo 状态等结构化信息全部丢失，orchestrator 只能看到一段文本摘要。

**修复建议**: 在 ToolResult 的 metadata 中携带结构化信息（如变更文件列表、todo 状态），供 orchestrator 后续使用。

---

### 🟢 P14：`brainstorming` 的 `skip_to_plan` 和 `small_change` exit 条件可能重叠

**位置**: `workflow/nodes.py:50-53`

**问题**: 两条 decision rules 都可能匹配"小范围但用户说 just implement it"的情况，优先级取决于 LLM 判断，没有确定性规则。

**修复建议**: 在 decision rules 中添加优先级或互斥条件说明。

---

## 五、闭环完整性矩阵

| 路径 | 闭环 | 问题 |
|------|------|------|
| 用户输入 → intent → workflow 激活 → gate → 工具执行 | ⚠️ | intent 变更无法撤销已激活 gate (P2) |
| workflow 节点 → advance_workflow → 后继激活 | ✅ | 基本闭环 |
| systematic-debugging → root cause → TDD → verification | ⚠️ | verification 无 gate (P7) |
| review → FAIL → receiving-code-review → TDD | ✅ | 闭环 |
| brainstorming → design → plan → TDD | ✅ | 闭环 |
| `done` exit → 结束 | ⚠️ | 隐式协议，无结构化定义 (P4) |
| compaction → gate 信息保留 | ⚠️ | workflow context 可能被删 (P3) |
| `agent implement` 委派 → 用户确认 | ❌ | 规则被覆盖，确认被绕过 (P1) |

---

## 六、修复优先级建议

| 优先级 | 问题 | 风险 | 工作量 |
|--------|------|------|--------|
| P0 | P1: `agent implement ask` 死代码 | 安全漏洞 | 小 |
| P0 | P2: Intent 变更无法撤销 gate | Agent 卡死 | 中 |
| P1 | P3: Compaction 可能丢失 gate | Gate 静默失效 | 中 |
| P1 | P4: `done` exit 无结构化定义 | 架构债务 | 中 |
| P2 | P5: `can_delegate` 死字段 | 语义混乱 | 小 |
| P2 | P7: verification 无 gate | 验证可被跳过 | 小 |
| P2 | P9: Decision Flow 与 gate 优先级 | LLM 误判 | 小 |
| P3 | P6: 子 agent 绕过状态管理 | 功能缺失 | 大 |
| P3 | P8: plan_mode 参数语义错误 | 重构风险 | 小 |
| P3 | P10-P14: 其余轻微问题 | 代码质量 | 小 |
