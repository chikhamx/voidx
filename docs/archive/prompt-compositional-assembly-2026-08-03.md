# Prompt 组合式装配

> **Status: Done** — Archived on 2026-08-04.

## 目标

把 `PromptPolicy` 从"以 coding 为底版打补丁的覆盖模式"改为"声明 profile-specific section 列表的组合模式"。消除 `llm_turn.py` 里的逐字段 `if prompt_policy.xxx is not None` 分支和 `_goal_phase_directive` / `_loop_phase_directive` 硬编码 if-else 链。

## 现状

### PromptPolicy 覆盖模式

`llm_turn.py:529-557` 逐字段检查 `prompt_policy`：
- `base_system_spec`：None=保留 coding 默认，否则覆盖
- `persona_prompt`：None=保留，`""`=抑制
- `workflow_runtime`：None=保留，`""`=抑制
- `task_state_section`：None=保留，`""`=抑制
- `profile_directive`：None=无，否则注入

只有 `CodingPromptPolicy`（全 None）和 `ChatPromptPolicy`（覆盖 base_system_spec + 抑制 persona/workflow/task_state + 注入 chat directive）。

### Phase directive 硬编码

goal/loop 没有 `PromptPolicy`。它们的 phase directive 通过 `llm_turn.py:80-109` 的 `_goal_phase_directive` / `_loop_phase_directive` 硬编码 if-else 链注入，拼接进 `profile_directive_value`。

### Section 装配

`RuntimeContextBuilder._build_stable_sections()` 按固定顺序拼 section：
```
Base System → Profile Directive → Persona → Workflow Runtime → Runtime State → Project Instructions → Session Time → Long Summary
```

### Stable-prefix 缓存

`build_incremental()` 对 stable section 列表算指纹（name+content），指纹不变则复用 `SystemMessage`。只要 section 列表内容和顺序不变，缓存命中。

## 设计

### 新 PromptPolicy 接口

```python
class PromptPolicy(Protocol):
    def base_system_spec(self) -> BaseSystemProfile | None:
        """Override Base System spec. None = keep coding default."""
        ...

    def profile_sections(self, turn_context: TurnExecutionContext | None) -> list[ContextSection]:
        """Profile-specific sections inserted after Base System.
        Can use turn_context to generate phase-dependent directives."""
        ...

    def suppress_sections(self) -> set[str]:
        """Default section names to suppress from the section list."""
        ...
```

三个方法替代原来的五个属性。`profile_sections(turn_context)` 是核心——它接收 turn_context，能根据 phase 动态生成 directive section。

### 各实现

#### CodingPromptPolicy

```python
class CodingPromptPolicy:
    def base_system_spec(self) -> None:
        return None
    def profile_sections(self, turn_context) -> list[ContextSection]:
        return []
    def suppress_sections(self) -> set[str]:
        return set()
```

#### ChatPromptPolicy

```python
class ChatPromptPolicy:
    def base_system_spec(self):
        return CHAT_PROFILE_SPEC
    def profile_sections(self, turn_context) -> list[ContextSection]:
        return [ContextSection(name="Profile Directive", content=_CHAT_DIRECTIVE)]
    def suppress_sections(self) -> set[str]:
        return {"Persona", "Workflow Runtime", "Current Task State"}
```

#### GoalPromptPolicy（新）

```python
class GoalPromptPolicy:
    def base_system_spec(self) -> None:
        return None
    def profile_sections(self, turn_context) -> list[ContextSection]:
        phase = getattr(turn_context, "goal_phase", "") if turn_context else ""
        directive = _goal_directive_for_phase(phase)  # intake/evaluator/idle → directive
        if not directive:
            return []
        return [ContextSection(name="Profile Directive", content=directive)]
    def suppress_sections(self) -> set[str]:
        return set()
```

`_goal_directive_for_phase` 把原来 `_goal_phase_directive` 的 if-else 逻辑搬进 `GoalPromptPolicy`。

#### LoopPromptPolicy（新）

同 GoalPromptPolicy，但用 `loop_phase` 和 `LOOP_IDLE_DIRECTIVE`。

### RuntimeContextBuilder 变更

构造参数从 `profile_directive: str` + `suppress_task_state: bool` 改为 `profile_sections: list[ContextSection]` + `suppress_sections: set[str]`。

`_build_stable_sections` 逻辑变更：
1. Base System（不变）
2. 插入 `profile_sections`（替代原来的单个 `profile_directive`）
3. Persona / Workflow Runtime（按 `suppress_sections` 过滤）
4. Runtime State / Project Instructions / Session Time / Long Summary（不变）

`_build_task_sections` 按 `suppress_sections` 是否含 `"Current Task State"` 过滤（替代 `suppress_task_state`）。

### llm_turn.py 变更

删除：
- `_goal_phase_directive` 函数（逻辑搬入 `GoalPromptPolicy`）
- `_loop_phase_directive` 函数（逻辑搬入 `LoopPromptPolicy`）
- 逐字段 `prompt_policy.xxx is not None` 检查（530-557）
- `profile_directive_value` 拼接（546-549）

替换为：
```python
profile_sections = (
    prompt_policy.profile_sections(turn_context)
    if prompt_policy is not None
    else []
)
suppress = (
    prompt_policy.suppress_sections()
    if prompt_policy is not None
    else set()
)
base_system_spec = (
    prompt_policy.base_system_spec()
    if prompt_policy is not None
    else CODING_PROFILE_SPEC
)
```

`RuntimeContextBuilder` 构造调用改为传 `profile_sections` / `suppress_sections`。

### Profile 定义变更

`GOAL_PROFILE` 和 `LOOP_PROFILE` 加 `prompt_policy=GoalPromptPolicy()` / `LoopPromptPolicy()`。

## 缓存安全

section 列表的内容和顺序不变 → 指纹不变 → 缓存命中。变更前后各 profile 的 section 列表完全等价：

| Profile | 变更前 | 变更后 |
|---|---|---|
| coding | Base System + Persona + Workflow Runtime + Runtime State + ... | 同（profile_sections=[], suppress=set()） |
| chat | Base System(chat) + Profile Directive(chat) + Runtime State + ... | 同（profile_sections=[chat directive], suppress={Persona, Workflow Runtime, Current Task State}） |
| goal idle | Base System + Profile Directive(idle) + Persona + Workflow Runtime + Runtime State + ... | 同（profile_sections=[idle directive], suppress=set()） |
| loop idle | Base System + Profile Directive(idle) + Persona + Workflow Runtime + Runtime State + ... | 同 |

## 实现任务

### Task 1: 新 PromptPolicy 接口 + 四个实现
- 文件: `src/voidx/agent/domain/prompt_policy.py`
- 改 `PromptPolicy` Protocol 为三方法接口
- 改 `CodingPromptPolicy` / `ChatPromptPolicy`
- 新增 `GoalPromptPolicy` / `LoopPromptPolicy`（搬入 phase directive 逻辑）
- 测试: `src/tests/test_domain/test_prompt_policy.py`

### Task 2: RuntimeContextBuilder 适配
- 文件: `src/voidx/agent/application/runtime_context.py`
- 构造参数: `profile_directive` + `suppress_task_state` → `profile_sections` + `suppress_sections`
- `_build_stable_sections`: 插入 profile_sections，按 suppress_sections 过滤
- `_build_task_sections`: 按 suppress_sections 过滤
- 测试: `src/tests/test_application/test_runtime_context_builder.py`

### Task 3: subagent.py 适配
- 文件: `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py:138`
- `RuntimeContextBuilder` 构造调用改为传 `profile_sections=[]` / `suppress_sections=set()`
- 测试: 现有 subagent 测试

### Task 4: llm_turn.py 适配
- 文件: `src/voidx/agent/infrastructure/langgraph/runtime/llm_turn.py`
- 删除 `_goal_phase_directive` / `_loop_phase_directive`
- 删除逐字段检查，改为三方法调用
- `RuntimeContextBuilder` 构造调用改为新参数
- 测试: 现有 `test_runtime_context_*` + `test_loop_protocol_injection` + `test_goal_protocol`

### Task 5: Profile 定义适配
- 文件: `src/voidx/agent/domain/goal.py` — `GOAL_PROFILE` 加 `prompt_policy=GoalPromptPolicy()`
- 文件: `src/voidx/agent/domain/loop.py` — `LOOP_PROFILE` 加 `prompt_policy=LoopPromptPolicy()`
- 测试: `src/tests/test_domain/test_loop_domain.py` + `src/tests/test_goal/test_goal_domain.py`

### Task 6: 全量回归
- 命令: `./test.py --backend -- src/tests/test_domain src/tests/test_application src/tests/test_infrastructure/runtime src/tests/test_goal src/tests/test_loop src/tests/test_tools -q`
- 预期: 全绿（排除已知的 `test_tool_result_preview` 2 个失败）

## 约束

- 不改 stable-prefix 缓存指纹算法
- 不改 `ContextSection` / `RuntimeContext` 数据模型
- 不改 `BaseSystemProfile` / `assemble_base_system` 逻辑
- 不改 `build_base_system` 语言覆盖逻辑
- `profile_sections` 返回的 section 顺序就是插入顺序（在 Base System 之后、Persona 之前）
- `suppress_sections` 用 section name 匹配（如 `"Persona"`, `"Workflow Runtime"`, `"Current Task State"`）

## 风险

- **缓存失效**：如果 section 内容或顺序意外变化，缓存失效但不影响正确性（只是性能降级）
- **section name 匹配**：`suppress_sections` 靠 name 字符串匹配，name 拼写错误会静默不过滤。用常量或测试覆盖
- **调用方同步**：`RuntimeContextBuilder` 构造参数变更，两个调用方都需适配——`llm_turn.py:598`（传 `prompt_policy.profile_sections()` / `suppress_sections()`）和 `subagent.py:138`（传 `profile_sections=[]` / `suppress_sections=set()`）
