> **Status: Done**
# Runtime Guard 工具意图感知 — 技术设计文档

Date: 2026-07-03

## Context

运行时间歇性出现 **false positive 导致 turn 提前终止**：Guard 把 `todo:write`、`workflow:advance` 等有状态变更的调用和 `todo:read` 一起归为"低价值重复工具"，在连续两轮被调用时触发 skip/terminate。

同时 `verify` 节点的 `passed_substantial` 出边缺少 auto-advance 检测，验证通过后依赖 LLM 显式调用 `workflow advance`，但 guard guidance 的措辞诱导 LLM 输出文本总结而非工具调用，造成 work → pause → re-prompt 的低效循环。

两个表象问题共享同一根因：**Guard 的进度判断只看工具名，不了解工具的具体操作语义。**

---

## Goals and Non-Goals

### Goals

- `todo:write` / `todo:update` 被视为有进度操作，不触发低价值重复守卫
- `workflow:enter` / `advance` / `done` 被视为有进度操作，不触发低价值重复守卫
- `todo` / `workflow` 的非只读调用贡献 evidence key，推进 no_progress 守卫的 reset
- `verify` 成功（bash exit 0 + test runner）自动推进到 `review` 的 `passed_substantial`

### Non-Goals

- 不改动 subagent 的 wall clock 守卫
- 不改动 tool failure loop 守卫（已基于 call key 去重）
- 不改动 `checkpoint` 的归类——它是 barrier 工具，天然需要用户输入，保留在低价值列表
- 不改造 `no_progress` 的 evidence key 哈希算法本身

---

## Architecture

三个独立但协同的改动，全部集中在 `src/voidx/agent/graph/runtime_guards.py`，外加 `src/voidx/workflow/auto_advance.py` 一条新检测规则。

```
调用方                              Guard 层                         Workflow 层
─────────                         ────────                        ────────────
tool_call {name, args} ──────────→ only_tool_key() ──→ RepetitiveToolCycleState
                                        │                   │
                                        │              is_stuck(only_tool_key ≠
                                        │                LOW_VALUE key → skip)
                                        │
                                        ↓
                                  cycle_summary()
                                        │
                                        ├─ has_progress ← todo:write/update
                                        │                  workflow:enter/advance/done
                                        │                  diff present
                                        │
                                        └─ evidence_keys ← todo:write/update args
                                                           workflow:enter/advance/done args

bash exit 0 + test runner ────────────────────────────→ auto_advance_events
                                                              │
                                                       passed_substantial
                                                       verify → review
```

---

## Data Model

### 新增: `tool_op_key(tool_call) → str`

```
tool_op_key({name: "todo", args: {op: "write"}}) → "todo:write"
tool_op_key({name: "todo", args: {op: "read"}})  → "todo:read"
tool_op_key({name: "workflow", args: {action: "advance"}}) → "workflow:advance"
tool_op_key({name: "bash", args: {command: "pytest"}})     → "bash"
```

op/action 提取规则:
- `todo` → `args.op`，缺失默认 `"read"`
- `workflow` → `args.action`，缺失默认 `""`
- 其他 → 直接 `tool_name`

### 修改: `LOW_VALUE_REPETITIVE_TOOLS`

```python
# 旧
LOW_VALUE_REPETITIVE_TOOLS = frozenset({"todo", "workflow", "checkpoint"})

# 新 — 按 tool_op_key 匹配
LOW_VALUE_REPETITIVE_TOOL_KEYS = frozenset({"todo:read", "checkpoint"})
```

`workflow` 的三种 action 都是状态变更，全部移出。
`todo:read` 保留，`checkpoint` 保留（是 barrier + 用户交互）。

### 修改: `only_tool_name` → `only_tool_key`

```python
# 旧: 只看 name
def only_tool_name(tool_calls) -> str

# 新: 看 name + op
def only_tool_key(tool_calls) -> str:
    keys = {tool_op_key(call) for call in tool_calls}
    if len(keys) == 1:
        return keys.pop()
    return ""
```

`is_stuck`、`decision_for_pending`、`record_cycle` 全部改用 `tool_op_key`。

### 修改: `cycle_summary_from_tools` 的 progress 判定

```python
# 新增: 非只读 todo/workflow 调用 = has_progress
for item in executed:
    tool_name = ...; args = ...
    if tool_name == "todo" and args.get("op") in ("write", "update"):
        has_progress = True
    if tool_name == "workflow" and args.get("action") in ("enter", "advance", "done"):
        has_progress = True
```

同时这些调用也正常贡献 evidence key（当前代码里 `todo`/`workflow` 被 `tool_name not in LOW_VALUE_REPETITIVE_TOOLS` 过滤掉了，改为按新 key 判断）。

### 新增: `verify → review` auto-advance

```python
# auto_advance.py 新增
def _check_verify_passed(metadata, active_names) -> WorkflowStateEvent | None:
    # bash exit_code == 0 + test runner matched + verify active
    # → passed_substantial
```

---

## Detailed Changes

### 文件 1: `src/voidx/agent/graph/runtime_guards.py`

| 变更 | 位置 | 内容 |
|------|------|------|
| 重构常量 | `LOW_VALUE_REPETITIVE_TOOLS` (L14) | 重命名为 `LOW_VALUE_REPETITIVE_TOOL_KEYS`，值改为 `frozenset({"todo:read", "checkpoint"})` |
| 新增函数 | 常量定义之后 | `def tool_op_key(tool_call) -> str` |
| 重命名函数 | `only_tool_name` (L437) | → `only_tool_key`，内部改用 `tool_op_key` |
| 更新 `is_stuck` | L155 | `only_tool` → `only_tool_key`，`LOW_VALUE_REPETITIVE_TOOLS` → `LOW_VALUE_REPETITIVE_TOOL_KEYS` |
| 更新 `decision_for_pending` | L161 | `only_tool_name` → `only_tool_key`，`LOW_VALUE_REPETITIVE_TOOLS` → `LOW_VALUE_REPETITIVE_TOOL_KEYS` |
| 更新 `record_cycle` | L125 | `only_tool_name` → `only_tool_key` |
| 更新 `cycle_summary_from_tools` | L393-413 | `only_tool_name` → `only_tool_key`；新增对 `todo:write/update` 和 `workflow:*` 设 `has_progress=True`；evidence key 过滤从 tool name 改为 key-based（`todo:read` 不贡献，`todo:write/update` 和 `workflow:*` 贡献） |
| 字段语义说明 | `ToolCycleSummary.only_tool` 定义 (L112) | 增加注释：改动后此字段实际存储 `tool_op_key`（如 `"todo:write"`），不再是纯 tool name |
| 更新 guidance 措辞 | L140, L178 | 当前措辞 `"or briefly explain what is blocking you"` 诱导 LLM 输出纯文本终止 turn — 这是 verify→pause→re-prompt 循环的关键推手。改为 `"call a concrete action tool next"`，不引导文本输出 |

### 文件 2: `src/voidx/workflow/auto_advance.py`

| 变更 | 位置 | 内容 |
|------|------|------|
| 新增函数 | `auto_advance_events` 附近 | `def _check_verify_passed(exit_code, command, active_names) -> WorkflowStateEvent \| None` — 需包含三步检查：(1) exit_code == 0 (2) command 匹配 test runner regex (3) `"verify" in active_names` (4) `DEFAULT_WORKFLOW_DAG.edges_from("verify")` 包含 `passed_substantial` 条件（防御性检查，与其他 auto-advance 函数一致） |
| 集成调用 | `auto_advance_events` 的 `bash`/`powershell` 分支 | 在 `_check_shell_result` 调用之后（即 exit ≠0 先命中 `failed_implementation`），增加 `events.extend(_check_verify_passed(...))` |

### 文件 3: 测试

| 文件 | 内容 |
|------|------|
| `tests/test_agent/graph/test_guards_tool_op.py` | 新建 — `tool_op_key` 各种组合、`only_tool_key` 去重、`cycle_summary` 的 progress/evidence 判定（含 `todo:write` 贡献 progress 但 `todo:read` 不贡献）、`is_stuck` 对 `"todo:write"` 不触发但对 `"todo:read"` 触发 |
| `tests/test_workflow/test_auto_advance.py` | 扩展 — `passed_substantial`：(a) bash exit 0 + pytest + verify active → event 产生 (b) bash exit 0 + non-test cmd → 无 event (c) bash exit 0 + verify 不活跃 → 无 event (d) metadata 缺 exit_code → 无 event (e) powershell 等价路径

---

## Error Handling & Edge Cases

| 场景 | 处理 |
|------|------|
| `todo` tool_call 缺少 `args.op` | `tool_op_key` 默认返回 `"todo:read"`（保守，宁可误判低价值也不漏杀） |
| `workflow` tool_call 缺少 `args.action` | `tool_op_key` 返回 `"workflow"`（老行为兼容） |
| `todo:read` 连续 2 轮 | 仍触发 skip（老行为，合理 — 纯查询不该卡循环） |
| `todo:write` 连续 2 轮 | 不触发 skip — `has_progress=True` 且 key 不在低价值集合 |
| `workflow:advance` 连续 2 轮 | 不触发 skip — `has_progress=True` 且 key 不在低价值集合 |
| `bash` 不含 test runner 命令且 exit 0 | `passed_substantial` 不触发（只有测试通过才算 verify 成功） |
| `bash` 不含 test runner 命令且 exit ≠0 | 不触发任何 auto-advance（非测试命令失败不是 verify 信号） |
| `bash` exit 0 + test runner，但 verify 不活跃 | `passed_substantial` 不触发 |
| metadata 缺 `exit_code` | `passed_substantial` 不触发 |
| `powershell` 等价于 bash 路径 | 与 bash 相同逻辑，共用 `_check_verify_passed` |
| `verify` 节点的 `failed_implementation` 仍优先 | bash exit ≠0 + test runner 先命中 `failed_implementation`，exit 0 才走 `passed_substantial` |
| `checkpoint` 保留在低价值列表 | checkpoint 是 barrier 工具，需要用户交互，连续等待不算进度 |
| `passed_substantial` 边未来被移除 | DAG 边存在性检查防止静默产生无效事件 |
| 一个 cycle 里同时有 `todo:write` + `todo:read` | `only_tool_key` 返回空串（多 key），不会触发 stuck 判定 |

---

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `todo:read` 保留在低价值 | 完全移除 todo | `todo:read` 是纯查询，连续做无意义 |
| `workflow:*` 全部移出低价值 | 只移出 `advance`/`done` | 三个 action 都有副作用，`enter` 也改变 DAG 状态 |
| `tool_op_key` 用字符串拼接而非 frozenset | `(name, op)` 元组做 key | 字符串更简单，和现有 `only_tool` 模式一致 |
| `passed_substantial` 靠 bash exit 0 + test runner | 靠 LLM 显式 workflow advance | 验证通过是最可靠的信号，无需 LLM 二次确认 |

---

## Open Questions

- [ ] `verify` 成功后是走 `passed_substantial` 还是新增一个 `passed_routine`？如果改动很小（比如 1 行），可能不需要 review。当前方案统一走 `passed_substantial`，后续根据实际体验调整。
- [ ] `todo:write` 的 full replace 可能写入和之前完全相同的内容——这算进度吗？当前方案算（因为 LLM 至少在尝试整理状态）。
