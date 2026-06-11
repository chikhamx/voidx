# Persona Prompt 架构重构 — 技术设计文档

## Context

voidx 当前有 5 个"角色"（orchestrator、explore、plan、implement、review），每个角色有独立的 prompt，以"You are voidx X"开头，暗示它们是不同的身份。这导致了一个根本性的认知错误：**voidx 不是 5 个角色，而是 1 个角色切换 4 种思维模式。**

当前问题：
- `orchestrator` 的 prompt 说"You are voidx orchestrator"——但 orchestrator 不是角色，是 voidx 本身在协调
- 子代理的 prompt 说"You are voidx explore"——但 explore 不是另一个角色，是 voidx 切换到了探查思维
- 用户感知上，他们始终在和 voidx 对话，不是在和 5 个不同的人对话
- prompt 中的"role"措辞强化了多角色幻觉，与产品定位矛盾

## Goals and Non-Goals

### Goals

- 重新设计 prompt 架构：1 个角色（voidx）+ 4 种思维模式（persona）
- 主循环 prompt：voidx 就是 voidx，不再自称 orchestrator
- 子代理 prompt：voidx 以特定思维模式工作，不是另一个角色
- 保持子代理架构不变（工具隔离、并行委派等机制保留）
- 保持 persona 名称不变：`explore`、`plan`、`implement`、`review`
- 更新 `AgentDef` 中的 `description`、`when_to_use` 反映 persona 定位

### Non-Goals

- 不改变 persona 名称（explore/plan/implement/review 保持不变）
- 不改变子代理架构（仍是 AgentDef + 委派机制）
- 不改变工具集分配（`on_intent` schema 除外，enum 值和 goal 字段必须改）
- 不改变隐藏 persona（compaction、title）
- 不兼容旧数据库记录（`agent_role` 字段中 `"orchestrator"` 值将失效，旧会话需清除）

## 核心概念

```
旧模型：5 个角色
  orchestrator ── 你是 voidx orchestrator
  explore      ── 你是 voidx explore
  plan         ── 你是 voidx plan
  implement    ── 你是 voidx implement
  review       ── 你是 voidx review

新模型：1 个角色 + 4 种思维模式
  voidx ── 切换 persona 来调整思维方式
    explore persona   ── 探查思维
    plan persona      ── 设计思维
    implement persona ── 构建思维
    review persona    ── 审视思维

  子代理 ── voidx 根据任务独立性决定是否委派
    委派时带上 persona，子代理以该思维模式执行
    不委派时，voidx 自己切换 persona 也能工作
```

关键区别：
- **角色** = 身份切换，"我变成了另一个人"
- **思维模式** = 视角切换，"我还是我，但换了个角度看问题"
- **子代理** = 执行策略，"这个任务够独立，派出去单独跑"

persona 和子代理是两个独立维度：
- persona 是思维模式，voidx 自己也能切换，不一定要委派
- 子代理是执行隔离，委派时附带 persona 指定思维模式
- 简单搜索：voidx 自己切到 explore persona 就行，不用委派
- 大范围重构：委派子代理 + implement persona，隔离执行

## Architecture

### Prompt 结构

```
┌─────────────────────────────────────┐
│ BASE_SYSTEM_PROMPT（不变）           │
│ - 沟通风格、全局规则、工作流运行时     │
└─────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────┐
│ 主循环 prompt（原 ORCHESTRATOR_PROMPT）│
│ - "You are voidx"                   │
│ - 决策流、委派规则、工具合约           │
│ - 不再自称 orchestrator              │
└─────────────────────────────────────┘
     │                    │
     │ 不委派             │ 委派子代理
     ▼                    ▼
┌──────────────┐  ┌──────────────────────┐
│ voidx 自己   │  │ 子代理               │
│ 切换 persona │  │ 附带 persona 指令    │
│ 直接执行     │  │ 隔离上下文单独执行    │
└──────────────┘  └──────────────────────┘
```

persona 是思维模式，不是子代理的同义词：
- voidx 自己也能切换 persona（简单搜索 → explore persona，不用委派）
- 子代理是执行隔离策略，委派时附带 persona 指定思维模式
- 两个维度独立组合

### 代码结构变更

`agents.py` 中的常量重命名（值也重写）：

| 旧常量名 | 新常量名 | 说明 |
|----------|---------|------|
| `ORCHESTRATOR_PROMPT` | `VOIDX_PROMPT` | 主循环 prompt，"You are voidx" |
| `EXPLORE_PROMPT` | `EXPLORE_PERSONA` | 探查思维模式 |
| `PLAN_PROMPT` | `PLAN_PERSONA` | 设计思维模式 |
| `IMPLEMENT_PROMPT` | `IMPLEMENT_PERSONA` | 构建思维模式 |
| `REVIEW_PROMPT` | `REVIEW_PERSONA` | 审视思维模式 |
| `ROLE_PROMPTS` | `PERSONA_PROMPTS` | 字典重命名，key 中 orchestrator→voidx |

`AgentDef` 字段语义更新：

| 字段 | 旧理解 | 新理解 |
|------|--------|--------|
| `name` | 角色名 | persona 标识符 |
| `description` | 角色描述 | 思维模式描述 |
| `when_to_use` | 何时使用该角色 | 何时切换到该思维模式 |
| `role_prompt` | 角色提示词 | 思维模式提示词 |

## Prompt 设计

### VOIDX_PROMPT（主循环）

```python
VOIDX_PROMPT = """You are voidx.

## Thinking Style

- Assess before acting. Understand the current state before deciding what to do next.
- You coordinate — you do not directly solve problems.
- Switch persona when the task demands a different thinking style.
- Delegate when the task is broad, risky, or needs isolation.
- Stay aligned with the user's actual goal, not their literal words.

## Your Responsibilities

- **Judge current state**: What has been done? What remains? Is anything blocked?
- **Judge goal**: What is the user actually trying to accomplish? Identify the goal's
  type (coding/analysis/design/review/chat), target (what to act on), and expected
  result (what success looks like). Intent is classified by runtime and defaults to
  coding; you judge the structured goal beyond the surface request.
- **Judge next step**: What should happen now — explore, plan, build, review, or respond?
- **Judge completion**: Is the task done? Verified? Ready to report?
- **Judge persona switch**: Should you switch to a different thinking style? Should you
  delegate to a sub-agent with a specific persona?
- **Judge tool need**: Do you need tools to gather information, or can you decide based on
  what you already know?

## Decision Flow

0. **Intent gate** — classify the latest user request:
   - Answer/explain → answer directly. No tools unless context is required.
   - Inspect/understand current state → use read/glob/grep/repo_map directly.
     Do not start plan→implement→review.
   - Discuss/design/propose → produce options or a plan. Do not implement unless
     the user explicitly approves.
   - Fix/implement/modify → unless blocked by an active workflow gate, edit
     directly for small scoped changes, or delegate broad/isolated work to
     implement persona.
   - Ambiguous → continue with read-only investigation when useful. Use clarify
     for one structured question before edits, unsafe bash, or implement delegation.

1. **Chat / explain** — just answer. No tools unless you need to look something up.
   If Current Task State says intent is chat or ambiguous, but the user request
   appears to require workspace action, call on_intent before other workspace tools.

2. **Simple search** — grab read/glob/grep and find it yourself. Only send explore
   persona for broad searches across many files.

3. **Design / plan** — hand off to plan persona for architecture questions. For
   non-trivial implementation plans, call plan_checkpoint before changing files,
   running write-capable commands, or delegating implement persona.

4. **Code changes**
   - Small documentation or single-file edits → unless blocked by an active
     workflow gate, read first, then call write/edit yourself and verify.
   - If investigation finds a concrete edit but the user asked only to inspect,
     design, or review, stop and report the proposed change. Ask for
     confirmation before editing.
   - Broad, risky, source/test/config, or multi-file patch work → unless blocked
     by an active workflow gate, use todo and delegate a complete brief to
     implement persona. Review non-trivial delegated work before reporting completion.
   - If review persona says FAIL or NEEDS_CHANGE → fix, review again.

5. **Unclear intent** — ask through clarify. One specific clarifying question is
   better than five assumptions.

## Rules

- Do not delegate to implement persona unless the user explicitly asks to modify code.
- In plan mode, do not call write/edit/lsp_format, unsafe bash, or implement delegation.
- Ambiguous implementation intent is not enough for write/edit/lsp_format,
  unsafe bash, or implement delegation.
- apply_patch is implement persona only. As the main loop, use write/edit for direct
  edits and delegate multi-file patch work to implement persona.
- Sub-agents do not interact with the user. If a sub-agent result needs user
  approval or clarification, call plan_checkpoint or clarify yourself.
- Don't tell the user "done" until changes are verified.
- Sub-agents have isolated context — give them complete, self-contained briefs.
- If Current Task State lists an active workflow gate, that workflow gate takes precedence over
  this decision flow. Do not delegate to implement persona or take implementation action while a gate
  blocks implementation workflows.
"""
```

### EXPLORE_PERSONA

```python
EXPLORE_PERSONA = """## Thinking Style

- Expand search space before narrowing.
- Prefer gathering evidence over assumptions.
- Look for unknowns and missing information.
- Consider alternative explanations.
- Avoid premature conclusions.
"""
```

### PLAN_PERSONA

```python
PLAN_PERSONA = """## Thinking Style

- Start from constraints and work toward structure.
- Prefer simple compositions over clever abstractions.
- Make dependencies explicit before proposing changes.
- Consider what must stay the same, not just what should change.
- Identify the smallest viable change that solves the problem.

## Output Structure

```
## Context
(What exists now, what needs to change)

## Approach
(2-3 sentences on the overall strategy)

## Steps
- [ ] Step 1: (specific file, specific change)
- [ ] Step 2: ...

## Affected Files
- path/to/file.py — (what changes here)

## Risks
- (edge cases, compatibility concerns)
```
"""
```

### IMPLEMENT_PERSONA

```python
IMPLEMENT_PERSONA = """## Thinking Style

- Prefer the smallest change that could work.
- Read before writing — understand before modifying.
- Make one change at a time, verify after each.
- Prefer mechanical transformations over creative rewrites.
- When uncertain, test rather than reason.

## Parallel Execution

- Tools in the same response run IN PARALLEL via asyncio.gather.
- Tools across separate responses run SEQUENTIALLY.
- Read multiple files before editing → batch reads in one response.
- Edit + verify test → two responses (edit first, then bash test).
"""
```

### REVIEW_PERSONA

```python
REVIEW_PERSONA = """## Thinking Style

- Assume the change is wrong until proven correct.
- Look for what's missing, not just what's present.
- Consider second-order effects beyond the immediate change.
- Distinguish style preferences from actual problems.
- Prefer specific evidence over general impressions.

## Checklist

- **Correctness**: Does the code do what was intended? Any logic bugs?
- **Completeness**: Edge cases handled? Error handling present?
- **Style**: Follows existing patterns and conventions?
- **Security**: Injection risks? Unsafe file operations? Hardcoded secrets?
- **Side effects**: What else might this change affect?

## Output Format

```
verdict: PASS | FAIL | NEEDS_CHANGE

## Issues
- [severity: critical/high/medium/low]
  file: path/to/file.py
  line: 42
  problem: (what's wrong)
  suggestion: (how to fix)
```

- Workflow impact: PASS leaves review workflow completion to the main loop.
  FAIL or NEEDS_CHANGE means the main loop should advance review with
  `review_has_issues` into review-feedback.
"""
```

## Data Model

### AgentDef

结构不变。字段语义更新：

```python
class AgentDef(BaseModel):
    name: str           # persona 标识符（orchestrator→voidx, 其余不变）
    description: str    # 思维模式描述（措辞更新）
    when_to_use: str    # 何时切换到该模式（措辞更新）
    tools: list[str]    # 不变
    can_write: bool     # 不变
    can_delegate: bool  # 不变
    max_steps: int      # 不变
    hidden: bool        # 不变
    model: str | None   # 不变
    mcp_tools: bool     # 不变
```

### Intent 与 Goal（重新分层）

当前 `TaskIntent` 把大类和细分混在一起（`chat`/`inspect`/`design`/`implement`/`debug`/`review`/`ambiguous`），
层次不清。重新划分为两层：

- **Intent**（`TaskIntent`）：大类，由 runtime 关键词分类器做初始判断。决定是否涉及代码操作。
  值：`coding` / `general`
- **Goal**（`Goal`）：具体用户需求目标，由 LLM 通过 `on_intent` 工具在第一轮返回结构化数据确定。
  在 `coding` intent 下细分目标类型。
  值：`bugfix` / `refactor` / `feature` / `chore` / `inspect` / `design` / `doc` 等

```python
class TaskIntent(str, Enum):
    CODING = "coding"     # 涉及代码操作（写、改、查、设计、修 bug……）
    GENERAL = "general"   # 通用交互（问答、闲聊、解释概念、非代码请求）

class GoalType(str, Enum):
    BUGFIX = "bugfix"         # 修 bug、排错
    REFACTOR = "refactor"     # 重构、结构优化
    FEATURE = "feature"       # 新功能、新特性
    CHORE = "chore"           # 杂项：配置、依赖、清理
    INSPECT = "inspect"       # 查看、分析、理解代码
    DESIGN = "design"         # 设计方案、架构规划
    DOC = "doc"               # 文档、注释、README

class Goal(BaseModel):
    type: GoalType = GoalType.FEATURE     # 目标类型，LLM 判断
    target: str = ""                       # 目标对象：要改什么、看什么、设计什么
    expected_result: str = ""              # 期望结果：完成后的状态描述
```

**层次关系**：
- `intent=coding` + `goal.type=bugfix` → 修 bug
- `intent=coding` + `goal.type=feature` → 写新功能
- `intent=coding` + `goal.type=inspect` → 看代码、分析
- `intent=coding` + `goal.type=design` → 设计方案
- `intent=coding` + `goal.type=doc` → 写文档
- `intent=general` → 通用交互，goal 通常为 None

**Goal 判定流程**：

Goal 不靠 runtime 关键词分类器猜，也不靠 `on_intent` 工具。而是让 voidx 在第一次 LLM 输出时
直接返回结构化的 goal 数据。如果 LLM 没输出结构化数据，就不进入工作流，让 LLM 自由发挥。

```
用户消息
  │
  ▼
runtime 关键词分类器 → intent（coding / general）
  │
  ▼
LLM 第一次输出，在回复开头附带结构化 goal 数据：
  <!-- goal:{"type":"bugfix","target":"auth.py","expected_result":"login no longer crashes on empty email"} -->
  │
  ├── 解析成功 → 构造 Goal 对象，写入 TaskState.current_goal
  │              → workflow_activations() 根据 intent + goal.type 选择工作流入口
  │
  └── 解析失败 / 未输出 → current_goal = None
                         → 不进入工作流，LLM 自由发挥
```

**实现方式**：

1. **VOIDX_PROMPT 中要求 LLM 输出 goal**：在 prompt 的 Decision Flow 第 0 步之前，
   要求 LLM 在每次回复用户时，先输出一行 HTML 注释格式的 goal 数据，再开始正常回复。

2. **runtime 解析 goal**：`turn_runner.py` 在收到 LLM 第一轮输出后，
   用正则 `<!-- goal:(\{.*?\}) -->` 提取 goal JSON，构造 `Goal` 对象。

3. **`on_intent` 工具暂时不用**：goal 结构化数据通过 LLM 输出直接获取，
   不需要额外工具调用。`on_intent` 工具保留但不作为 goal 判定的主要路径。

**prompt 中的 goal 输出要求**（加在 VOIDX_PROMPT 的 Decision Flow 之前）：

```
## Goal Output

Before responding to the user, output a goal annotation on the first line of your reply:

<!-- goal:{"type":"<goal_type>","target":"<what>","expected_result":"<success>"} -->

- type: bugfix | refactor | feature | chore | inspect | design | doc
- target: what to act on (file, module, system, etc.)
- expected_result: what success looks like after completion
- For general conversation (intent=general), omit the goal annotation entirely.
```

**关键约束**：
- `intent=coding` 时，LLM 应输出 goal 注释；如果没输出，`current_goal = None`，不进入工作流
- `intent=general` 时，LLM 不输出 goal 注释，`current_goal = None`
- goal 注释对用户不可见（HTML 注释格式，TUI/Web 渲染时过滤）
- `on_intent` 工具保留，但暂不作为 goal 判定的主要路径，未来可作为 fallback 或强制覆盖

**`TaskState` 字段结构**：

```python
class TaskState(BaseModel):
    current_intent: TaskIntent = TaskIntent.CODING  # 大类，runtime 判断
    previous_intent: TaskIntent | None = None
    current_goal: Goal | None = None                # 具体目标，LLM 判断
    pending_approval: PendingApproval | None = None
    last_plan_summary: str = ""
    recent_user_texts: list[str] = Field(default_factory=list)
    todo_state: TodoRunState | None = None
```

**变更影响**：

| 位置 | 变更 |
|------|------|
| `TaskIntent` 枚举 | `CHAT`/`INSPECT`/`DESIGN`/`REVIEW`/`IMPLEMENT`/`DEBUG`/`AMBIGUOUS` → `CODING`/`GENERAL` |
| `TaskState.current_goal: str` | → `current_goal: Goal \| None = None` |
| `TaskRun.goal: str` | → `goal: Goal \| None = None` |
| `ToolStatePatch.goal: str \| None` | → `goal: Goal \| None = None` |
| `AgentState.goal: str` | → `goal: str`（序列化后的 JSON 字符串，LangGraph 兼容） |
| `TaskState.update_after_turn` | `_summarize_scope(goal_text)` → 构造 `Goal` 对象 |
| `TaskRun.set_goal` | 同上 |
| `infer_task_intent()` | 简化为判断 coding vs chat |
| `intent.py` 中的 `_IMPLEMENT_HINTS`/`_DESIGN_HINTS` 等 | 合并为 `_CODING_HINTS`，细分逻辑移入 goal 分类 |
| 所有读取 `current_intent` 做 `IMPLEMENT`/`DEBUG`/`DESIGN` 等判断的地方 | 改为读 `current_goal.type` |
| 所有读取 `current_goal` / `task_run.goal` 的地方 | 适配 `Goal` 对象或 `.target` 属性 |

### `name` 字段变更

唯一变更：`"orchestrator"` → `"voidx"`

其余 persona 名称（`explore`、`plan`、`implement`、`review`）保持不变。

### 数据库处理

不兼容旧数据。`context_frames.agent_role` 中已有的 `"orchestrator"` 值将失效，
旧会话记录需清除。不做双读映射，不做迁移脚本。

## API Contract

### `role_prompt_for_llm` → `persona_prompt_for_llm`

- **变更前**: `if agent.name != "orchestrator"` 硬编码
- **变更后**: `if not agent.can_delegate`（语义等价，更健壮，消除硬编码）

### `AgentDef.role_prompt` → `AgentDef.persona_prompt`

属性重命名，逻辑不变：

```python
@property
def persona_prompt(self) -> str:
    if self.name in PROMPTLESS_AGENTS:
        return ""
    try:
        return PERSONA_PROMPTS[self.name]
    except KeyError as exc:
        raise ValueError(f"No persona prompt registered for: {self.name}") from exc
```

### `context_frames` 读写

- **写入**: `agent_role` 字段，主循环写 `"voidx"`，其余不变
- **读取**: 不兼容旧值 `"orchestrator"`，旧会话需清除

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 数据库中存在 `"orchestrator"` | 不兼容，旧会话需清除 |
| 代码中硬编码 `"orchestrator"` 字符串 | 全量更新为 `"voidx"` |
| 测试断言使用旧名称 | 测试代码统一更新 |

## 变更清单

### `src/voidx/agent/agents.py`

| 变更 | 说明 |
|------|------|
| `ORCHESTRATOR_PROMPT` → `VOIDX_PROMPT` | 常量重命名 + 内容重写 |
| `EXPLORE_PROMPT` → `EXPLORE_PERSONA` | 常量重命名 + 内容重写 |
| `PLAN_PROMPT` → `PLAN_PERSONA` | 常量重命名 + 内容重写 |
| `IMPLEMENT_PROMPT` → `IMPLEMENT_PERSONA` | 常量重命名 + 内容重写 |
| `REVIEW_PROMPT` → `REVIEW_PERSONA` | 常量重命名 + 内容重写 |
| `ROLE_PROMPTS` → `PERSONA_PROMPTS` | 字典重命名，key 中 `"orchestrator"` → `"voidx"` |
| `PROMPTLESS_AGENTS` | 不变 |
| `AgentDef.role_prompt` → `AgentDef.persona_prompt` | 属性重命名 |
| `AgentDef.tool_contract` 中 `"- Role: {self.name}"` | → `"- Persona: {self.name}"` |
| `BUILTIN_AGENTS["orchestrator"]` → `BUILTIN_AGENTS["voidx"]` | key + name 字段更新 |
| `BUILTIN_AGENTS` 各项 `description` / `when_to_use` | 措辞更新：角色 → 思维模式 |
| `role_prompt_for_llm` → `persona_prompt_for_llm` | 函数重命名 + 硬编码更新 |
| `get_subagents()` docstring | 更新措辞 |
| 模块 docstring | 更新：5 agents → 1 agent + 4 personas |
| `BASE_SYSTEM_PROMPT` 中 "Do not expose internal role names" | → "Do not expose internal persona names" |

### `src/voidx/runtime/intent.py`

| 变更 | 说明 |
|------|------|
| `TaskIntent` 枚举 | `CHAT`/`INSPECT`/`DESIGN`/`REVIEW`/`IMPLEMENT`/`DEBUG`/`AMBIGUOUS` → `CODING`/`GENERAL` |
| `infer_task_intent()` | 简化为判断 coding vs chat |
| `_IMPLEMENT_HINTS`/`_DESIGN_HINTS`/`_INSPECT_HINTS`/`_REVIEW_HINTS`/`_DEBUG_HINTS` | 合并为 `_CODING_HINTS`，细分逻辑移入 goal 分类 |
| 所有引用 `TaskIntent.IMPLEMENT`/`DEBUG`/`DESIGN`/`INSPECT`/`REVIEW`/`AMBIGUOUS` 的地方 | 改为 `TaskIntent.CODING` + 读 `goal.type` |

### `src/voidx/runtime/task_state.py`

| 变更 | 说明 |
|------|------|
| 新增 `GoalType` 枚举 | `BUGFIX`/`REFACTOR`/`FEATURE`/`CHORE`/`INSPECT`/`DESIGN`/`DOC` |
| 新增 `Goal` 模型 | `type: GoalType`、`target: str`、`expected_result: str` |
| `TaskState.current_goal: str` | → `current_goal: Goal \| None = None` |
| `TaskRun.goal: str` | → `goal: Goal \| None = None` |
| `TaskRun.active` 属性 | `bool(self.goal)` → `self.goal is not None and bool(self.goal.target)` |
| `TaskRun.set_goal` | 接受 `Goal` 对象或从字符串构造（兼容过渡） |
| `TaskRun.clear` | `self.goal = ""` → `self.goal = None` |
| `ToolStatePatch.goal: str \| None` | → `goal: Goal \| None = None` |
| `TaskState.update_after_turn` | `_summarize_scope(goal_text)` → 构造 `Goal` 对象 |
| 所有读取 `current_intent` 做 `IMPLEMENT`/`DEBUG`/`DESIGN` 等判断的地方 | 改为读 `current_goal.type` |
| 所有读取 `current_goal` / `task_run.goal` 的地方 | 适配 `Goal` 对象或 `.target` 属性 |

### `src/voidx/agent/state.py`

| 变更 | 说明 |
|------|------|
| `agent` 字段注释 `orchestrator/explore/plan/implement/review` | → `voidx/explore/plan/implement/review`；主循环时值为 `"voidx"`，子代理时为 persona 名 |
| `goal: str` | 保持 `str`（LangGraph 兼容），值为 `Goal.model_dump_json()` 序列化结果 |

### `src/voidx/runtime/intent_classifier.py`

| 变更 | 说明 |
|------|------|
| `ArtifactClassifier.safe_accept_intents` | 排除 IMPLEMENT 的逻辑 → 排除 CODING（或移除，intent 只有两值无需排除） |
| `ArtifactClassifier.classify()` | 对 IMPLEMENT 的特殊处理 → 改为读 `goal.type` |
| `classify_intent()` | keyword fallback 到各旧枚举值 → 简化为 coding vs general |

### `src/voidx/agent/intent_refinement.py`

| 变更 | 说明 |
|------|------|
| `refine_intent()` | 全部分支按旧枚举值判断 → 改为 intent 层判断 coding/general，goal 层判断 type |
| `_confirm_intent()` | 同上 |
| `_available_tools_for_intent()` | 按 IMPLEMENT/DESIGN/INSPECT 等分配工具 → 改为按 `goal.type` 分配 |
| `_pending_approval_for_intent()` | 按 IMPLEMENT/DESIGN 等生成审批 → 改为按 `goal.type` 生成 |

### `src/voidx/tools/on_intent.py`

| 变更 | 说明 |
|------|------|
| `OnIntentInput.intent` schema enum | `chat`/`inspect`/`design`/`implement`/`debug`/`ambiguous` → `coding`/`general` |
| `OnIntentResult` | 新增 `goal` 相关字段，`on_intent` 工具现在也要设置 goal |
| `OnIntentResult.confirmed_intent` | 返回值从旧枚举 → `CODING`/`GENERAL` |

### `src/voidx/memory/runtime_state.py`

| 变更 | 说明 |
|------|------|
| `MessageRuntimeSnapshot.task_intent` | 默认 `CHAT` → `CODING` |
| `save_session_runtime_state()` | IMPLEMENT 判断 → 改为读 `goal.type` |
| `current_goal` 字段 | 从字符串读写 → Goal 对象序列化/反序列化 |

### `src/voidx/memory/store.py`

| 变更 | 说明 |
|------|------|
| `current_goal TEXT NOT NULL DEFAULT ''` | → `current_goal TEXT` 存 Goal JSON，默认 NULL |
| `agent_role` DEFAULT | `'orchestrator'` → `'voidx'`（已在原清单中） |

### `src/voidx/agent/slash/handler.py`

| 变更 | 说明 |
|------|------|
| `/goal` 命令 | 直接读写 `task_run.goal` 字符串 → 能设置 Goal 的 type/target/expected_result |

### `src/voidx/agent/` 其余文件

| 文件 | 变更 |
|------|------|
| `graph/core.py` | `role_prompt_for_llm` → `persona_prompt_for_llm`；`role_prompt` 变量 → `persona_prompt`；`state.get("agent", "orchestrator")` → `state.get("agent", "voidx")`（3 处）；`state.get("goal")` 传给 RuntimeContextBuilder 和 workflow context → Goal JSON 反序列化适配 |
| `graph/subagent.py` | `agent_def.role_prompt` → `agent_def.persona_prompt` |
| `runtime_context.py` | `role_prompt` 字段 → `persona_prompt`；`goal` 参数从字符串 → Goal 对象适配；`_current_task_state()` 中 DESIGN 判断 → 改为读 `goal.type`；`task_intent` 默认值 `CHAT` → `CODING` |
| `graph/compaction_coordinator.py` | 不变（`agent_role="compaction"` 不变） |
| `graph/tool_executor.py` | `state.get("agent", "orchestrator")` → `state.get("agent", "voidx")`；`runtime_goal = state.get("goal", "")` → Goal JSON 反序列化；`runtime_task_intent = state.get("task_intent", "chat")` 默认值 → `"coding"`；`ToolStatePatch.goal` 从字符串 → Goal 对象适配 |
| `graph/topology.py` | `state.get("agent", "orchestrator")` → `state.get("agent", "voidx")` |
| `graph/turn_runner.py` | `"max_steps": _resolve_max_steps(host.config, "orchestrator")` → `"voidx"`；`"agent": "orchestrator"` → `"voidx"`；`_resolve_max_steps` 第二个参数 `"orchestrator"` → `"voidx"`；`task_intent=task_intent.value` → 新枚举值适配；`task_run.goal` 判断 goal mode → Goal 对象适配；构建 AgentState `"goal"` 字段 → `Goal.model_dump_json()` |
| `tools/agent.py` | `if agent_name == "orchestrator"` → `if agent_name == "voidx"`（阻止 voidx 作为子代理运行） |

### `src/voidx/memory/` 文件

| 文件 | 变更 |
|------|------|
| `context_frames.py` | `agent_role` 默认值 `"orchestrator"` → `"voidx"`（3 处） |
| `store.py` | `agent_role TEXT NOT NULL DEFAULT 'orchestrator'` → `DEFAULT 'voidx'` |

### `src/voidx/ui/` 文件

| 文件 | 变更 |
|------|------|
| `output/agent_display.py` | `"orchestrator": "voidx"` key → `"voidx": "voidx"`；其余 persona 显示名更新 |
| `output/console/app.py` | `"orchestrator": "thinking"` → `"voidx": "thinking"`；`agent == "orchestrator"` → `agent == "voidx"` |

### `src/voidx/workflow/schema.py`

| 变更 | 说明 |
|------|------|
| `IntentEntry.intent: str` | → `intent: str` + `goal_type: str = ""` 组合判断 |
| `IntentEntry` validator | `_normalize_intent` 不变；新增 `_normalize_goal_type` |
| `WorkflowDAG.intent_map` 语义 | 从 intent 单维度入口 → intent + goal_type 组合入口 |

### `src/voidx/workflow/dag.py`

| 变更 | 说明 |
|------|------|
| `IntentEntry(intent="debug", ...)` | → `IntentEntry(intent="coding", goal_type="bugfix", nodes=["debug", "tdd", "verify"], reason="bugfix goal")` |
| `IntentEntry(intent="implement", ...)` | → `IntentEntry(intent="coding", goal_type="feature", nodes=["tdd", "verify"], reason="feature goal")` |
| `IntentEntry(intent="design", ...)` | → `IntentEntry(intent="coding", goal_type="design", nodes=["brainstorm"], reason="design goal")` |
| `IntentEntry(intent="review", ...)` | → `IntentEntry(intent="coding", goal_type="inspect", nodes=["review"], reason="inspect goal")` |
| 新增 `IntentEntry` | `intent="coding", goal_type="refactor"` → tdd + verify |
| 新增 `IntentEntry` | `intent="coding", goal_type="chore"` → tdd + verify |
| 新增 `IntentEntry` | `intent="coding", goal_type="doc"` → tdd + verify（doc 也走 TDD，纯文档可走 allowed_exceptions） |

**新 `intent_map` 完整定义**：

```python
intent_map=[
    IntentEntry(intent="coding", goal_type="bugfix", nodes=["debug", "tdd", "verify"], reason="bugfix goal"),
    IntentEntry(intent="coding", goal_type="feature", nodes=["tdd", "verify"], reason="feature goal"),
    IntentEntry(intent="coding", goal_type="refactor", nodes=["tdd", "verify"], reason="refactor goal"),
    IntentEntry(intent="coding", goal_type="chore", nodes=["tdd", "verify"], reason="chore goal"),
    IntentEntry(intent="coding", goal_type="design", nodes=["brainstorm"], reason="design goal"),
    IntentEntry(intent="coding", goal_type="inspect", nodes=["review"], reason="inspect goal"),
    IntentEntry(intent="coding", goal_type="doc", nodes=["tdd", "verify"], reason="doc goal"),
]
```

**`intent=general` 不激活任何工作流**——纯对话不需要结构化流程。

### `src/voidx/workflow/policy.py`

| 变更 | 说明 |
|------|------|
| `workflow_activations()` 签名 | 新增 `goal_type: str \| None = None` 参数 |
| `if intent == "debug"` 分支 | → `if intent == "coding" and goal_type == "bugfix"` |
| `if intent == "implement"` 分支 | → `if intent == "coding" and goal_type in ("feature", "refactor", "chore", "doc")` |
| `if intent == "review"` 分支 | → `if intent == "coding" and goal_type == "inspect"` |
| `if intent == "design"` 分支 | → `if intent == "coding" and goal_type == "design"` |
| `if agent_name == "implement"` 分支 | 保留（子代理 persona 仍可触发工作流） |
| `if agent_name == "plan"` 分支 | 保留 |
| `if mode == "plan"` 分支 | 保留 |
| `intent=general` | 不激活任何工作流 |

**新 `workflow_activations()` 核心逻辑**：

```python
def workflow_activations(
    user_text: str,
    *,
    agent: str = "",
    task_intent: str | None = None,
    goal_type: str | None = None,
    interaction_mode: str | None = None,
) -> list[WorkflowActivation]:
    ...
    intent = (task_intent or "").strip().lower()
    gtype = (goal_type or "").strip().lower()

    if intent == "coding" and gtype == "bugfix":
        add("debug", "bugfix goal")
        add("tdd", "bugfix fix lifecycle")
        add("verify", "bugfix lifecycle")

    if intent == "coding" and gtype in ("feature", "refactor", "chore", "doc"):
        add("tdd", f"{gtype} goal")
        add("verify", f"{gtype} lifecycle")

    if intent == "coding" and gtype == "inspect":
        if _contains_any(text, _REVIEW_FEEDBACK_TERMS):
            add("review-feedback", "review feedback")
        else:
            add("review", "inspect goal")

    if intent == "coding" and gtype == "design":
        add("brainstorm", "design goal")
        if _contains_any(text, _PLAN_TERMS):
            add("plan", "planning intent")

    # agent persona 仍可触发
    if agent_name == "implement":
        add("tdd", "implement persona")
        add("verify", "implement lifecycle")
    if agent_name == "plan":
        add("plan", "plan persona")

    if mode == "plan":
        add("brainstorm", "plan mode")
        add("plan", "plan mode")
```

### `src/voidx/workflow/service.py`

| 变更 | 说明 |
|------|------|
| 调用 `workflow_activations()` 的地方 | 传入 `goal_type` 参数 |
| trigger 匹配逻辑 | 不变（关键词触发仍按 triggers 列表） |

### `src/voidx/agent/tool_filters.py`

| 变更 | 说明 |
|------|------|
| 模块 docstring "Worker roles" | → "Worker personas" |

### 测试 + 文档

| 范围 | 变更 |
|------|------|
| `tests/` 中 `role_prompt` 引用 | → `persona_prompt` |
| `tests/` 中 `"orchestrator"` 字符串断言 | → `"voidx"` |
| `tests/` 中 `ROLE_PROMPTS` 引用 | → `PERSONA_PROMPTS` |
| `AGENTS.md` | 更新项目说明：5 agents → 1 agent + 4 personas |
| `docs/` 中引用 role 的文档 | 措辞更新 |

## 可干掉项

新分层下，以下旧结构变得多余或语义重叠，建议在本次改造中一并移除：

### `TaskIntent.AMBIGUOUS`

旧模型中 AMBIGUOUS 表示“不确定是哪种意图”，但新分层只有 `coding`/`general` 两个大类，
不存在模糊空间。模棱两可的话默认归 `GENERAL` 即可。

**影响**：
- `resolve_turn_intent()` 中“approval phrase without a pending plan → AMBIGUOUS”→ 直接返回 `GENERAL`
- `intent_refinement.py` 中所有 `AMBIGUOUS` 分支 → 移除
- `on_intent.py` schema enum 中移除 `ambiguous`
- `task_state.py` 中 `_phase_for_intent()` 的 AMBIGUOUS 分支 → 随 TaskPhase 一起干掉

### `TaskPhase` 枚举

当前 `TaskPhase` 有 CLARIFY/INSPECT/DESIGN/IMPLEMENT/VERIFY/REVIEW/DONE，
与旧 TaskIntent 枚举高度重叠。新分层后，phase 的语义被 `intent` + `goal.type` 取代：
- `intent=coding + goal.type=bugfix` 已经表达了“在修 bug”
- `intent=coding + goal.type=design` 已经表达了“在设计”
- 不需要再有个 IMPLEMENT/DESIGN phase

**建议**：干掉 `TaskPhase`，或简化为 `active`/`done` 两个状态（如果仍需要标记任务是否完成）。

**影响**：
- `TaskRun.phase` 字段 → 移除或简化
- `TaskRun.update_after_turn()` 中 phase 更新逻辑 → 移除
- 所有读取 `task_run.phase` 的地方 → 改为读 `goal.type`

### `_phase_for_intent()` 函数

把 intent 映射到 phase 的辅助函数，随 TaskPhase 一起干掉。

### `PendingApproval.source_intent`

当前 `source_intent: TaskIntent` 记录审批来源的 intent 类型。新分层后 intent 只有 coding/general，
信息量很低。建议改为 `source_goal_type: GoalType`，更有意义——知道审批来自 bugfix 还是 feature
比知道来自 coding 更有用。

**影响**：
- `PendingApproval.source_intent: TaskIntent` → `source_goal_type: GoalType`
- 所有构造 `PendingApproval` 的地方 → 传 `goal.type` 而非 `intent`
- 所有读取 `source_intent` 的地方 → 改为读 `source_goal_type`

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 主循环名称 `voidx` | 保持 `orchestrator` | 品牌即角色，用户说 voidx 就是指它 |
| Persona 名称不变 | 重命名为 scout/architect/builder/critic | 用户指定保持原名，名称本身足够清晰 |
| 保持子代理架构 | 合并为单一代理 | 混合方案：主循环是 voidx，需要隔离/并行时委派子代理 |
| `role_prompt` → `persona_prompt` | 保持 `role_prompt` | 代码层面也统一到 persona 语义，避免 role 残留造成混淆 |
| 数据库不兼容旧数据 | 双读映射 `_normalize_agent_role()` | 用户明确要求不兼容，避免代码中长期残留兼容逻辑 |
| `role_prompt_for_llm` 改用 `can_delegate` | 保持硬编码字符串 | 语义等价但更健壮，未来新增可委派 persona 时不会遗漏 |
| Intent 重新分层：`coding`/`general` | 保持 7 值枚举 | 大类和细分混在一起层次不清；intent 只回答“是否涉及代码”，细分由 goal.type 负责 |
| `Goal` 结构化：`type`/`target`/`expected_result` | 保持纯字符串 | 字符串无法表达目标结构，LLM 需要明确 type/target/expected_result 才能准确判断 |
| 干掉 `TaskIntent.AMBIGUOUS` | 保留，默认归 GENERAL | 新分层只有两个大类，不存在模糊空间；模棱两可归 GENERAL 即可 |
| 干掉 `TaskPhase` 枚举 | 保留 | 与 intent+goal.type 重叠，phase 语义被新分层取代；如需标记完成状态可简化为 active/done |
| `PendingApproval.source_intent` → `source_goal_type` | 保持 `source_intent` | intent 只有 coding/general 信息量低；goal.type（bugfix/feature 等）对审批来源更有意义 |
| `on_intent` 工具 schema 必须改 | 不改工具 schema | enum 值从 7 个变 2 个，且需新增 goal 字段；不改则工具无法正确设置新分层 |

## Open Questions

- [ ] `BASE_SYSTEM_PROMPT` 中 "Do not expose internal persona names unless the user asks about architecture" 是否需要保留？既然 persona 是思维模式而非角色，暴露名称是否更自然？
