# agent_name 语义清理

> **Status: In Progress**

## 问题

`agent_name` 在代码中承载了三种不同的语义，变量名相同但含义不同，导致多处混用和潜在 bug。

## 语义定义

| 语义 | 命名 | 值域 | 说明 |
|------|------|------|------|
| Agent 定义标识 | `agent_id` / `agent_def_name` | `"voidx"` | `AgentDef.name`，标识 agent 定义。用户/API 可见层只保留 `voidx` |
| 运行时角色 | `persona` / `runtime_persona` | `"coordinate"`, `"explore"`, `"plan"`, `"implement"`, `"review"` | 当前激活的思考模式 |
| UI 显示名 | `display_name` / `role_name` | `"Explorer"`, `"Planner"`, `"Implementer"`, `"Reviewer"` 等 | `agent_display_name()` 的输出，仅用于展示 |

**规则**：`agent_name` 这个变量名不再使用。新代码必须按上述三列命名。

## Agent Identity 简化

`sub-voidx` 不再作为用户/API 可见值存在。系统只有一个 agent definition：`AgentDef.name == "voidx"`。

子 agent 仍然是独立运行上下文，但不通过第二个 agent identity 表达：

- `agent` tool 的 `agent` 参数默认/固定为 `"voidx"`；旧的 `"sub-voidx"` 和 `"explore"` / `"implement"` 等 persona 简写不再作为公开 API。
- 子运行使用 `voidx` 的 base system prompt 和 `VOIDX_PROMPT`，保证 prompt 前缀尽量一致。
- 子运行的隔离约束放在独立的 `Runtime Constraints` section：不能直接和用户交互、不能再启动 child agent、只完成委派任务、按 runtime persona 执行。
- 工具权限和 UI 展示继续依赖 `runtime_persona`，不依赖 agent definition name。

## 误用清单

### P0 — 会导致错误行为

#### 1. `subagent.py:251` — authorize 传入 agent_def.name 而非 persona

```python
# 当前
approved, denied = await authorize_tools(assistant_msg.tool_calls, agent_def.name)
# agent_def.name 是 agent definition id，但 authorize 的 agent_name 参数期望 persona

# 修复：authorize callback 不接收 agent identity，只使用 runtime_persona 闭包
approved, denied = await authorize_tools(assistant_msg.tool_calls)
```

`persona` 变量在 `_run_subagent` 的参数中已有，直接使用即可。

#### 2. `permissions.py:62` — agent_name 作为 runtime_persona 的回退

```python
# 当前
and _persona_requires_approval(classified.capability, runtime_persona or agent_name)
# 当 runtime_persona=None 时回退到 agent_name（agent definition id）
# agent definition id 不应参与 persona 权限判断

# 修复：移除回退，默认 "coordinate"
and _persona_requires_approval(classified.capability, runtime_persona or "coordinate")
```

#### 3. `permissions.py:29` — agent_name 参数语义错误

```python
# 当前
async def _authorize_tool_calls(
    self, tool_calls, agent_name: str, ...
)

# 修复：移除 agent_name 参数，只保留 runtime_persona
async def _authorize_tool_calls(
    self, tool_calls, *, runtime_persona: str = "coordinate", ...
)
```

同步修改 `contracts.py:92,118` 的 Protocol 定义。

### P1 — 语义混淆，当前碰巧正确

#### 4. `core.py:754` — persona 赋值给 agent_name

```python
# 当前
agent_name = state.get("persona", "coordinate")

# 修复
persona = state.get("persona", "coordinate")
# 下游 agent_persona=agent_name → agent_persona=persona
```

#### 5. `subagent.py:305,309` — 函数参数名与语义不符

```python
# 当前
def _task_intent_for_agent(agent_name: str) -> str:
def _goal_type_for_agent(agent_name: str, task_description: str = "") -> str:

# 修复
def _task_intent_for_agent(persona: str) -> str:
def _goal_type_for_agent(persona: str, task_description: str = "") -> str:
```

调用者已经传 persona 值，只需改参数名。

#### 6. `policy.py:47` — 局部变量名与语义不符

```python
# 当前
agent_name = (agent or "").strip().lower()

# 修复
persona = (agent or "").strip().lower()
# 第 78, 84 行的 agent_name → persona
```

#### 7. `core.py:530` — authorize 闭包参数名

```python
# 当前
async def authorize(calls, agent_name: str):
    return await self._authorize_tool_calls(calls, agent_name=agent_name, ...)

# 修复：与 P0-3 联动，移除 agent_name 参数
async def authorize(calls):
    return await self._authorize_tool_calls(calls, runtime_persona=runtime_persona, ...)
```

### P2 — 数据不准确，当前不影响行为

#### 8. `core.py:545` — SubagentStarted.name 传 agent_def.name

```python
# 当前
SubagentStarted(name=agent_def.name, ...)  # "voidx"，不是用户真正看到的 runtime role

# 修复：传 persona，UI 层能拿到真实角色
SubagentStarted(name=runtime_persona, ...)
```

同步修改 `consumers.py:283` 中 `payload["agent_name"]` 的语义注释。

#### 9. `tool_executor.py:78` — 主循环硬编码

```python
# 当前
agent_name = "voidx"

# 修复：随 P0-3 移除 agent_name，authorize 调用只传 runtime_persona
# runtime_persona 已在第 79 行获取
```

#### 10. `tool_executor.py:570,578` — _authorize_tool_calls 辅助函数

```python
# 当前
async def _authorize_tool_calls(authorize, tool_calls, *, agent_name: str, ...):
    kwargs = {"agent_name": agent_name, ...}

# 修复：参数改为 runtime_persona
async def _authorize_tool_calls(authorize, tool_calls, *, runtime_persona: str = "coordinate", ...):
    kwargs = {"runtime_persona": runtime_persona, ...}
```

## 不需要修改的位置

以下位置的 `agent_name` 语义正确，是 UI 展示用途：

- `ui/output/tree.py:44,61,64` — TreeNode 的展示字段
- `ui/output/capture.py:55` — capture 层赋值展示名
- `ui/output/events/consumers.py:279,308,357` — 事件消费层展示名
- `ui/output/dock/nodes.py:311` — dock 展示名
- `ui/transcript.py:48,113` — 序列化/反序列化 TreeNode
- `ui/protocol/transcript.py:26,75` — 协议层 TranscriptNode

这些位置的 `agent_name` 含义是"展示用的角色名"，属于 UI 数据字段，不是逻辑变量。可以保留字段名不变（它是数据 schema 的一部分），但建议在 TreeNode 的 `agent_name` 字段加注释说明其语义为 display name。

## 修复计划

1. **Phase 1 — P0 修复**（必须）
   - 修改 `permissions.py`：移除 `agent_name` 参数，`runtime_persona` 默认 `"coordinate"`
   - 修改 `contracts.py`：同步 Protocol 定义
   - 修改 `subagent.py:251`：authorize callback 不再传 `agent_def.name`
   - 修改 `tool_executor.py`：移除 `agent_name` 变量，authorize 调用只传 `runtime_persona`
   - 修改 `core.py:530`：authorize 闭包移除 `agent_name` 参数

2. **Phase 2 — P1 重命名**（建议）
   - `core.py:754`：`agent_name` → `persona`
   - `subagent.py:305,309`：参数名 `agent_name` → `persona`
   - `policy.py:47`：局部变量 `agent_name` → `persona`

3. **Phase 3 — P2 数据修正**（建议）
   - `core.py:545`：`SubagentStarted(name=runtime_persona)`
   - TreeNode `agent_name` 字段加注释

4. **Phase 4 — 单一 agent identity**（必须）
   - 删除 `sub-voidx` builtin agent definition 和 prompt。
   - `AgentInput.agent` 默认值改为 `"voidx"`，schema/description 不再暴露 `sub-voidx`。
   - `get_subagents()` 返回 child-run view of `voidx`，仅供 agent tool 描述和 allowlist 使用。
   - `run_subagent()` 入口把传入 `AgentDef` 转成 child-run view：`name="voidx"`、`can_delegate=False`、嵌套 `agent` tool 被过滤。
   - `RuntimeContextBuilder` 支持 `runtime_constraints` section；child run 将约束放在该 section，而不是 fork 一份 `sub-voidx` prompt。
   - 默认 permission rule 从 `agent=sub-voidx allow` 改为 `agent=voidx allow`；`persona=implement` 继续走 `agent=implement ask`。

## 测试要点

- Phase 1 后：implement persona 子 agent 写文件不应弹审批（当前可能误弹）
- Phase 1 后：coordinate persona 主循环写文件应弹审批
- Phase 1 后：所有现有测试通过
- Phase 2 后：grep `agent_name` 在 `src/voidx/agent/` 和 `src/voidx/workflow/` 中应只剩 UI 层引用
- Phase 4 后：`get_agent("sub-voidx") is None`
- Phase 4 后：`AgentInput` schema 不包含 `sub-voidx`
- Phase 4 后：child run system context 包含 `Agent Role` 的 `VOIDX_PROMPT` 和独立 `Runtime Constraints`
