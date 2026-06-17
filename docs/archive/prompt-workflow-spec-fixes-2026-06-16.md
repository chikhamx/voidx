# Prompt & Workflow Spec 修复规格

> **Status**: Done
> **Date**: 2026-06-16
> **Scope**: 系统提示词、工具描述、Workflow 节点定义、Persona 描述、Goal Resolver

---

## Context

voidx 的 LLM 行为由四层提示词驱动：系统提示词（BASE_SYSTEM_PROMPT + VOIDX_PROMPT）、工具描述（tool description + parameters）、Workflow 节点定义（nodes.py → render → context message）、Persona 描述（agents.py）。这四层之间存在描述不一致、语义冲突、语言混用等问题，导致 LLM 在边界场景下行为不可预测。

本次修复目标是消除所有 P0/P1 级别的不一致，统一语言和语义，不改变架构。

## Goals and Non-Goals

### Goals

- 消除所有 P0 级别的描述与实现不一致
- 消除所有 P1 级别的语义模糊和冲突
- 统一 Workflow 节点 io/goal 字段语言为英文
- 补全缺失的概念定义（persona thinking mode）
- 修复 DAG 中缺失的边和 goal_map 入口

### Non-Goals

- 不重构提示词架构或消息组装逻辑
- 不改变 Workflow DAG 的节点拓扑（不增删节点，但可新增边）
- 不修改工具的执行逻辑，只改描述
- 不处理 P2 及以下的优化建议

## 修复项清单

### P0: 描述与实现不一致

#### P0-1. `todo` 工具描述声称有 `id` 字段但模型没有

- **文件**: `src/voidx/tools/todo.py`
- **现状**: 描述说 `Items:[{id, status, content}]`，但 `TodoItem` model 只有 `content` 和 `status`
- **修复**: 描述改为 `Items:[{status, content}]`，删除 `id` 引用
- **验证**: `grep -n "id" src/voidx/tools/todo.py` 确认描述中无 id

#### P0-2. `git` 工具未加入 voidx agent 的 tools 列表

- **文件**: `src/voidx/agent/agents.py`
- **现状**: `BUILTIN_AGENTS["voidx"].tools` 不包含 `"git"`，但 `ToolRegistry._register_builtins` 注册了 `GitTool`
- **修复**: 在 `BUILTIN_AGENTS["voidx"].tools` 中加入 `"git"`
- **验证**: 启动 voidx，调用 git 工具确认可用

#### P0-3. `plan` 节点 gate 的 denied_tools 和 allowed_paths 语义冲突

- **文件**: `src/voidx/workflow/nodes.py`
- **现状**: `plan` 节点 `denied_tools=("write", "edit")` 且 `allowed_paths=("docs/specs/**", "docs/design/**")`。LLM 看到 denied_tools 会认为完全禁止 write/edit，不知道 allowed_paths 是豁免
- **修复**: gate description 改为 `"Do not write implementation code until the plan is approved. Write/edit is allowed only under docs/specs/ and docs/design/ for plan documents."`。同时确认 render 输出中 gate description 出现在 denied_tools 列表之前，确保 LLM 优先看到豁免说明
- **验证**: 检查 render 输出中 gate 描述包含 allowed_paths 豁免说明

#### P0-4. `verify` 节点引用不存在的 `passed_substantial` exit condition

- **文件**: `src/voidx/workflow/nodes.py`
- **现状**: rules 中说 `"Use done instead of passed_substantial when verification passed but the change is small or routine."`，但 `passed_substantial` 不是任何 exit condition
- **修复**: 删除此规则。`passed_substantial` 将通过 P1-8 新增的 `verify -> review` 边作为 exit condition 重新引入，此处仅删除 rules 中对不存在条件的引用
- **关联**: P1-8 新增 `verify -> review` 边时使用 `condition="passed_substantial"`，使该条件名从 rules 引用变为正式的 exit condition
- **验证**: `grep -n "passed_substantial" src/voidx/workflow/nodes.py` 返回空

#### P0-5. `debug` 节点 subworkflow 要求实现修复但 gate 禁止写操作

- **文件**: `src/voidx/workflow/nodes.py`
- **现状**: debug 的 `gate.denied_tools=("write", "edit")`，但 subworkflow step 6 说 "Implement the smallest supported fix"，step 7 说 "Run reproduction and broader tests"
- **修复**: 删除 subworkflow step 6 和 7，将 step 5 改为 "Test the hypothesis minimally (read-only: inspect logs, add print statements via read, check config)"。subworkflow 专注于根因定位
- **关联**: 同时将 `io.output.fix`（"修复内容"）改为 `io.output.fix_direction`（"Fix direction description"），与 debug 只定位不修复的语义一致（见 P1-6 映射表）
- **验证**: debug subworkflow 只包含定位步骤，无写操作

#### P0-6. `debug` subworkflow exit_condition 与 gate 约束矛盾

- **文件**: `src/voidx/workflow/nodes.py`
- **现状**: exit_condition 说 `"root cause confirmed and original symptom no longer reproduces"`，但 debug 阶段不能修复问题，所以 "symptom no longer reproduces" 不可达
- **修复**: exit_condition 改为 `"root cause confirmed and fix direction is known"`
- **关联**: 同时将 `io.output.fix`（"修复内容"）改为 `io.output.fix_direction`（"Fix direction description"），与 P0-5 一致（见 P1-6 映射表）
- **验证**: exit_condition 不再要求 symptom 消失

#### P0-7. `inspect` goal type 没有入口节点

- **文件**: `src/voidx/workflow/dag.py`
- **现状**: `GoalEntry(goal_type="inspect", nodes=[], reason="goal:inspect")`，nodes 为空
- **修复**: 改为 `GoalEntry(goal_type="inspect", nodes=["brainstorm"], reason="goal:inspect")`——inspect 走 brainstorm 做分析，用户可随时 `done` 退出
- **验证**: goal resolver 选择 inspect 时能正确路由到 brainstorm

### P1: 语义模糊和冲突

#### P1-1. BASE_SYSTEM_PROMPT 中 "the decision flow below" 指代不明

- **文件**: `src/voidx/agent/agents.py`
- **现状**: `"that workflow gate takes precedence over persona prompts, delegation rules, and the decision flow below."` — BASE_SYSTEM_PROMPT 中没有 "decision flow"
- **修复**: 改为 `"that workflow gate takes precedence over persona prompts and delegation rules."`
- **验证**: 搜索确认无 "decision flow" 残留

#### P1-2. explore persona 缺少 read-only 说明

- **文件**: `src/voidx/agent/agents.py`
- **现状**: explore 描述只说 "Evidence gathering and codebase search"，没说 read-only
- **修复**: 改为 `"explore": Read-only evidence gathering and codebase search. Search broadly, report with concrete paths and lines. Do not write or edit files.`
- **验证**: explore persona 描述包含 read-only 约束

#### P1-3. review persona verdict 类型——只保留 PASS/FAIL

- **文件**: `src/voidx/agent/agents.py`, `src/voidx/workflow/nodes.py`
- **现状**: review 描述说 "Produce PASS/FAIL verdicts"，但 review workflow node 的 io.output.review_result 写的是 `审查结果(PASS/FAIL/NEEDS_CHANGE)`，存在不一致
- **修复**: 统一为只保留 PASS/FAIL。修改 review workflow node 的 `io.output.review_result` 为 `Review result (PASS/FAIL)`。review 节点的 rules 和 feedback 节点的路由逻辑不变——FAIL 即触发 feedback，PASS 即完成。NEEDS_CHANGE 语义与 FAIL 重叠，删除可减少 LLM 犹豫
- **验证**: `grep -n "NEEDS_CHANGE" src/voidx/workflow/nodes.py` 返回空；persona 描述和 node io 一致

#### P1-4. CHILD_RUN_CONSTRAINTS 输出格式与 agent tool result.format 冲突

- **文件**: `src/voidx/agent/agents.py`
- **现状**: 说 "Structure your final output with clear sections: what you found, what you did, and what remains uncertain."，但 agent tool 的 `result.format` 已定义结构化输出
- **修复**: 改为 `"Follow the structured result format specified in the agent tool call for your final output."`
- **验证**: child run 约束不再与 result.format 冲突

#### P1-5. ~~barrier tool 概念未在系统提示词中定义~~ 不需要

- **文件**: `src/voidx/agent/agents.py`
- **现状**: `clarify` 和 `plan_checkpoint` 描述中说 "This is a barrier tool"，但 BASE_SYSTEM_PROMPT 没有定义这个概念
- **结论**: 不需要向 LLM 解释 barrier tool 概念。工具描述中的 "later tool calls in the same response are deferred" 已足够表达行为语义。从 `clarify` 和 `plan_checkpoint` 的描述中删除 "This is a barrier tool" 这句话
- **验证**: `grep -n "barrier tool" src/voidx/tools/clarify.py src/voidx/tools/plan_checkpoint.py` 返回空

#### P1-6. Workflow 节点 io/goal 字段语言不一致

- **文件**: `src/voidx/workflow/nodes.py`
- **现状**: `description` 和 `rules` 用英文，`io.input`/`io.output` 和 `goal` 用中文
- **修复**: 将所有 `io.input`、`io.output`、`goal` 字段改为英文。具体映射:

| 节点 | 字段 | 当前（中文） | 改为（英文） |
|------|------|-------------|-------------|
| brainstorm | goal | 确认需求和设计方案，获得用户批准 | Confirm requirements and design, get user approval |
| brainstorm | io.input.user_request | 用户原始请求 | User's original request |
| brainstorm | io.output.design | 批准的设计方案或确认的变更范围 | Approved design or confirmed change scope |
| brainstorm | io.output.scope | 确认的变更边界 | Confirmed change boundaries |
| design-doc | goal | 产出通过读者测试的结构化文档 | Produce a structured document that passes the reader test |
| design-doc | io.input.design | 批准的设计方案 | Approved design |
| design-doc | io.input.doc_type | 文档类型(prd/tech-design/rfc/api-doc/readme) | Document type (prd/tech-design/rfc/api-doc/readme) |
| design-doc | io.output.doc_path | 文档保存路径 | Document save path |
| design-doc | io.output.doc_type | 实际文档类型 | Actual document type |
| plan | goal | 产出可执行的实施计划，获得用户批准 | Produce an executable implementation plan, get user approval |
| plan | io.input.spec | 设计文档或需求规格 | Design document or requirements spec |
| plan | io.input.scope | 变更范围 | Change scope |
| plan | io.output.plan | 实施计划，含任务列表、文件结构、测试定义 | Implementation plan with task list, file structure, test definitions |
| plan | io.output.tasks | 有序任务清单 | Ordered task list |
| plan | io.output.test_commands | 相关验证命令 | Related verification commands |
| tdd | goal | 按 TDD 循环完成实现，测试全绿 | Complete implementation via TDD cycle, all tests green |
| tdd | io.input.plan | 实施计划 | Implementation plan |
| tdd | io.input.task | 当前要实现的任务 | Current task to implement |
| tdd | io.output.files_changed | 修改的文件列表 | List of changed files |
| tdd | io.output.tests_written | 编写的测试列表 | List of tests written |
| tdd | io.output.test_result | 测试运行结果 | Test run result |
| verify | goal | 用可复现的证据证明变更达到预期状态 | Prove changes reach expected state with reproducible evidence |
| verify | io.input.claim | 声称完成的状态(done/fixed/passing) | Claimed completion status (done/fixed/passing) |
| verify | io.input.files_changed | 变更文件 | Changed files |
| verify | io.input.test_commands | 相关测试命令 | Related test commands |
| verify | io.output.evidence | 验证证据，包含命令和输出 | Verification evidence including commands and output |
| verify | io.output.verified | 是否通过 | Whether verified |
| verify | io.output.scope | 变更影响范围(substantial/routine) | Change impact scope (substantial/routine) |
| review | goal | 发起结构化的代码审查请求并收集 verdict | Initiate structured code review request and collect verdict |
| review | io.input.files_changed | 变更文件 | Changed files |
| review | io.input.verification_evidence | 验证证据 | Verification evidence |
| review | io.input.risks | 风险点 | Risk points |
| review | io.output.review_brief | 审查简报 | Review brief |
| review | io.output.review_result | 审查结果(PASS/FAIL/NEEDS_CHANGE) | Review result (PASS/FAIL) |
| feedback | goal | 验证并实施有效的审查反馈 | Verify and implement valid review feedback |
| feedback | io.input.feedback | 审查反馈内容 | Review feedback content |
| feedback | io.input.source | 反馈来源(human/external) | Feedback source (human/external) |
| feedback | io.output.changes_made | 根据反馈做的变更 | Changes made based on feedback |
| feedback | io.output.feedback_status | 每条反馈的处理状态(accepted/rejected/deferred) | Per-item feedback status (accepted/rejected/deferred) |
| feedback | io.output.deferred_items | 需要设计、分析或规划而非直接实施的反馈项 | Items needing design/analysis/planning rather than direct implementation |
| debug | goal | 定位根因并修复，验证修复有效 | Locate root cause and confirm fix direction |
| debug | io.input.error | 错误信息或异常表现 | Error message or abnormal behavior |
| debug | io.input.scenario | 问题发生的场景和上下文 | Scenario and context where the problem occurs |
| debug | io.input.reproduction | 复现步骤 | Reproduction steps |
| debug | io.output.root_cause | 根因描述 | Root cause description |
| debug | io.output.fix_direction | 修复方向描述 | Fix direction description |
| debug | io.output.fix_type | 修复类型(trivial/nontrivial) | Fix type (trivial/nontrivial) |

- **验证**: `python -c "import re; t=open('src/voidx/workflow/nodes.py').read(); print(len(re.findall(r'[\\u4e00-\\u9fff]', t)))"` 确认 io/goal 字段无中文

#### P1-7. Persona 是 thinking mode 不是 separate agent 未说明

- **文件**: `src/voidx/agent/agents.py`
- **现状**: Persona Model 部分说 "voidx has five thinking modes (personas)" 但没说它们是同一 agent 内的模式切换
- **修复**: 在 Persona Model 部分加一行: `"- Personas are thinking modes within the same agent, not separate agents. The runtime updates the active persona when workflow nodes change."`
- **验证**: Persona Model 描述包含 thinking mode 说明

#### P1-8. 缺少 `verify -> review` 边

- **文件**: `src/voidx/workflow/dag.py`
- **现状**: 验证通过后如果变更是 substantial，没有路径触发 review
- **修复**: 新增 `Edge(source="verify", target="review", condition="passed_substantial", label="verification passed with substantial changes", description="Use when verification passes and the change is substantial enough to warrant review.")`。`verify` 节点的 `render_node_markdown` 输出中 exits 列表会自动包含此边，无需额外修改节点定义
- **关联**: 此边使 `passed_substantial` 成为正式 exit condition，与 P0-4 删除 rules 中对该名称的引用形成配合——P0-4 删除了无定义的引用，P1-8 补上了定义
- **验证**: verify 节点有 `passed_substantial` exit condition 指向 review

#### P1-9. ~~`chore` goal type 没有入口节点~~ 已确认存在

- **文件**: `src/voidx/workflow/dag.py`
- **现状**: `GoalEntry(goal_type="chore", nodes=["tdd", "verify"], reason="goal:chore")` — 已有入口，无需修复
- **结论**: 审查时遗漏，此项关闭

#### P1-10. ~~`bash` 工具描述中 "No comments inside the command" 缺少理由~~ 删除此描述

- **文件**: `src/voidx/tools/bash.py`
- **现状**: 描述说 "No comments inside the command" 但没解释为什么
- **结论**: 直接删除 "No comments inside the command"。描述改为 `"Execute a shell command in the workspace directory. Returns stdout, stderr, and exit code."`
- **确认**: `bash.py` 的 `_BLOCKED` 列表和 `_normalize_command` 均无注释过滤逻辑，删除描述不影响运行时行为
- **验证**: bash 工具描述中无 "No comments"

#### P1-11. ~~MCP 工具概念未在系统提示词中提及~~ 不需要

- **文件**: `src/voidx/agent/agents.py`
- **现状**: LLM 看到 MCP 工具但系统提示词没提 MCP
- **结论**: 不需要在系统提示词中提及 MCP。LLM 通过工具列表和描述自然理解 MCP 工具的用途，无需额外概念解释
- **验证**: 无需修改

#### P1-12. `load_skills` 工具 "normalized skill name" 概念不清

- **文件**: `src/voidx/tools/load_skills.py`
- **现状**: 参数描述说 "Use normalized skill names only" 但没解释什么是 normalized
- **修复**: 改为 `"Skill names to load. Use normalized skill names only, not paths. Normalized names are lowercase, hyphen-separated (e.g. 'react-patterns'). At most {_MAX_SKILL_NAMES} names."`
- **验证**: 参数描述包含 normalized name 示例

#### P1-13. `feedback` 节点 step 6 描述过长，嵌入路由逻辑

- **文件**: `src/voidx/workflow/nodes.py`
- **现状**: step 6 description 包含 needs_design 和 needs_plan 的路由逻辑
- **修复**: 简化为 `"One coherent item at a time. Route items needing design to needs_design; items needing planning to needs_plan."`
- **验证**: step 6 描述简洁，路由逻辑由 edges 表达

#### P1-14. `agent` 工具描述过于冗长，需要精简

- **文件**: `src/voidx/tools/agent.py`
- **现状**: 描述约 10 行，重复了 VOIDX_PROMPT 中的调度规则，且包含大量 LLM 不需要的实现细节
- **修复**: 精简为：`"Start an isolated child agent for a delegated task. Use ONLY when you need to run multiple independent tasks in parallel, or the user explicitly asks for a child agent. Do not use for single-file reads, simple searches, or straightforward tasks you can do directly. Each call must include goal_resolution.goal, goal_resolution.plan.join, goal_resolution.plan.leave, and result.format. The child agent receives your task description and runtime context, but not caller conversation history."`
- **注意**: `parallel_subagents_enabled=True` 时动态追加的并发说明（`agent.py:77-82`）保持不变，精简只影响基础描述
- **验证**: agent 工具基础描述不超过 5 行

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| io/goal 字段统一英文 | 保留中文 | 英文 LLM 对英文描述理解更准确，减少歧义 |
| debug 不允许写操作 | 允许 debug 写 trivial fix | 保持 debug 专注定位，修复走 tdd/verify 更安全 |
| 新增 verify->review 边 | 保持现状，review 需手动触发 | substantial 变更应自动触发 review，防止遗漏 |
| inspect 走 brainstorm | 新增 inspect 专属节点 | inspect 本质是分析，brainstorm 已有 explore persona 和 read-only 工具 |
| review verdict 只保留 PASS/FAIL | 保留 NEEDS_CHANGE | NEEDS_CHANGE 与 FAIL 语义重叠，删除减少 LLM 犹豫 |
| 不向 LLM 解释 barrier tool | 在系统提示词中定义 | 工具描述中的行为语义已足够，无需额外概念 |
| 删除 bash "No comments" 描述 | 补充理由 | 约束无实际意义，删除更简洁 |
| 不提及 MCP 概念 | 在系统提示词中说明 | LLM 通过工具列表自然理解，无需额外解释 |
| agent 工具描述精简 | 保持现状 | 重复调度规则浪费 token，精简后更清晰 |
| debug io.output.fix → fix_direction | 保留 fix | debug 不允许写操作，输出"修复内容"语义矛盾，改为 fix_direction 与 P0-5/P0-6 一致 |

## Open Questions

- [ ] goal resolver 的 `_ALLOWED_JOIN_NODES` 是否应从 DAG goal_map 动态生成？（P2，本次不修）
