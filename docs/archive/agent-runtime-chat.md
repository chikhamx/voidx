> **Status: Done** — Archived on 2026-07-26.

---
name: agent-runtime-chat
display_name: Agent Runtime Chat 模式设计
description: 在统一 AgentRuntime 基础上增加独立 chat profile、thread 和工具策略
doc_type: tech-design
audience: human+llm
status: proposed
source_design: docs/design/agent-runtime-unification.md
---

# Agent Runtime Chat 模式设计

## 1. Summary

本设计在现有 `AgentRuntime.run_turn()` 基础上增加 chat 模式。Chat 是一个独立的 runtime profile 和 conversation thread，不创建第二套 LangGraph executor、TurnRunner 或 runtime state owner。

第一版 chat 允许：

- 普通对话；
- 本地 workspace/file 的只读查询；
- Web 搜索和 Web 内容抓取；
- MCP 工具调用，不区分 MCP 工具的读写属性；
- 未来显式接入的其他 chat 工具。

第一版 chat 禁止：

- `bash`；
- `powershell`；
- 本地文件写入、修改、删除和移动；
- git 写操作；
- 其他明确会修改 workspace 或本地系统状态的非 MCP 工具；
- 通过 `agent`/subagent 间接绕过上述本地工具边界。

MCP 不在 chat profile 中按只读/写入分类；MCP 的可用性由现有 MCP catalog、连接状态、工具注册和现有 MCP 权限/错误语义决定。Chat profile 不额外推断或改写 MCP 工具能力。

## 2. Goals and Non-goals

### Goals

1. 让 chat 复用 `AgentRuntime.run_turn(TurnRequest)` 完成单轮执行。
2. 让 chat 拥有独立 thread、conversation transcript、runtime state、compaction state 和 tool execution context。
3. 通过 profile/resource view 强制 chat 的本地工具边界，而不是只依赖 system prompt。
4. 保持现有 coding 的 prompt、工具、权限、session、slash 和 LangGraph 行为不变。
5. 允许 chat 恢复自己的 conversation，并将最终 session/thread identity 通过 `TurnResult` 返回。
6. 为后续 goal、workflow、loop 复用 profile、thread、resource view 和 lifecycle 契约。

### Non-goals

- 本阶段不实现 loop scheduler、自动唤醒或周期执行。
- 本阶段不实现 goal/workflow lifecycle。
- 本阶段不实现多 chat 并发、recovery、retry 或跨 thread 共享状态。
- 本阶段不重写 LangGraph topology、tool loop、compaction 算法或 MCP adapter。
- 本阶段不新增前端协议；UI/API 适配另列任务。
- 本阶段不改变 MCP 工具的读写分类或替 MCP 增加 capability 标注。

## 3. Current Reusable Boundary

当前 coding production path：

```text
AgentService
  → AgentRuntime.run_turn(TurnRequest)
  → LangGraphTurnEngine
  → LangGraphExecution.run_turn()
  → TurnRunner
```

相关现有契约和路径：

- `src/voidx/agent/runtime/runtime.py`：单轮 runtime facade；
- `src/voidx/agent/runtime/contracts.py`：`TurnRequest`、`TurnResult`；
- `src/voidx/agent/domain/profile.py`：`RuntimeProfile`；
- `src/voidx/agent/domain/thread.py`：`AgentThread`、`LifecycleState`；
- `src/voidx/agent/domain/state.py`：`SessionRuntimeState`；
- `src/voidx/agent/ports/runtime_resources.py`：runtime dependencies；
- `src/voidx/agent/infrastructure/langgraph/adapter.py`：runtime state 与 LangGraph execution 的 adapter；
- `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py`：graph turn、消息/transcript 和 LangGraph infrastructure persistence。

Chat 必须调用上述同一条执行链，不得在 application 或 frontend 中直接调用 `LangGraphExecution`。

## 4. Runtime Profile

现有 `RuntimeProfile` 目前只表达 profile identity。Chat Phase 2A 应增加最小的不可变 policy 字段；不要在 `AgentRuntime` 中加入 `if profile_id == "chat"` 的业务分支。

建议契约：

```python
class RuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str
    revision: int = Field(ge=1)
    name: str
    tool_policy: ToolPolicy
    prompt_policy: PromptPolicy
    interaction_policy: InteractionPolicy
```

如果现有 profile 扩展需要兼容 coding，coding 应显式提供默认 policy，而不是依赖 `None` 表示隐含规则。

### Chat profile defaults

```python
CHAT_PROFILE = RuntimeProfile(
    profile_id="chat",
    revision=1,
    name="Chat",
    tool_policy=ChatToolPolicy(),
    prompt_policy=ChatPromptPolicy(),
    interaction_policy=ChatInteractionPolicy(),
)
```

Chat policy 的本地工具规则：

```text
allow:
  - read-only filesystem inspection already supported by the tool registry
  - websearch
  - webfetch
  - MCP tools registered by the existing MCP integration

deny:
  - bash
  - powershell
  - local write/edit/delete/move tools
  - git mutation tools
  - agent/subagent tools when they can reach denied local capabilities
  - unknown non-MCP tools without an explicit chat allow rule
```

“只读”只适用于本地工具和非 MCP 工具。MCP 工具不执行本设计新增的 read/write 分类。

## 5. Thread and Persistence Model

Chat 不借用 coding session 作为 transcript 或 runtime state owner。每个 chat conversation 都有自己的 `AgentThread`：

```python
AgentThread(
    thread_id="chat:<conversation_id>",
    session_id="<chat_session_id>",
    parent_thread_id=None,
    lifecycle=LifecycleState.CREATED,
)
```

Chat-owned data：

- user/assistant/tool transcript；
- `SessionRuntimeState`；
- compaction summary；
- conversation metadata/title；
- chat tool execution context；

Chat 工具不进入 coding 的审批或权限授予流程。ChatService 创建 thread 时绑定固定的 chat tool view；一次 turn 只能调用该 view 中已经注册且符合资源范围的工具。未绑定工具、越界资源或不满足只读约束的调用直接返回稳定的 `tool-denied` 结果，不向用户发起审批，也不转换为 coding 权限请求。

可共享但不可共享可变状态：

```text
shared:
  model/provider clients
  tool definitions and registry catalog
  MCP catalog and connection configuration
  skill catalog
  compaction implementation

chat-scoped:
  selected tool view
  resource scope and policy guard state
  transcript/session state
  compaction summary
  tool call records
```

Chat tool view 的绑定范围包括：

- 无 workspace：仅绑定普通对话、Web 工具和现有 MCP 工具，不绑定本地文件工具；
- 有 workspace：额外绑定该目录范围内的本地只读工具；
- 所有情况下：不绑定 shell、写入/编辑/删除/移动、git mutation 和 agent/subagent 工具。
借用 workspace 只表示 chat 可以读取 workspace 资源；不得因此把 chat 的 transcript、runtime state、todo、workflow 或 compaction 写入 coding session。

Session store 需要支持按 chat `session_id` 加载和保存 runtime state。Chat 的新 conversation 可以 lazy 创建 session，最终 identity 必须由 `TurnResult.session_id` 返回，而不能由宿主 coding session 推断。

## 6. Chat Application Boundary

新增 `ChatService`，建议路径：

```text
src/voidx/agent/application/chat_service.py
```

`ChatService` 负责：

1. 创建或恢复 chat `AgentThread`；
2. 选择 `CHAT_PROFILE`；
3. 根据 workspace 和 profile 绑定固定的 chat tool view；
4. 构造 `TurnRequest`；
5. 绑定 chat-scoped `RuntimeResources`/resource view；
6. 调用 `AgentRuntime.run_turn()`；
7. 返回 `TurnResult` 和 chat-facing transcript result。

`ChatService` 不负责：

- 直接调用 LangGraph；
- 直接运行 tool；
- 直接保存 runtime state、message/transcript 或 turn event；
- 复制 coding 的 `TurnService` 或第二套执行器；
- 把 chat 输入伪装成 coding slash 或 synthetic turn；
- 发起、等待或处理 coding 工具审批。

建议接口：

```python
class ChatService:
    async def run_turn(
        self,
        thread: AgentThread,
        user_text: str,
        runtime: SessionRuntimeState | None = None,
        *,
        context: Any | None = None,
    ) -> TurnResult: ...
```

内部唯一执行调用：

```python
return await self._runtime.run_turn(
    TurnRequest(
        thread=thread,
        user_text=user_text,
        profile=CHAT_PROFILE,
        runtime=runtime or SessionRuntimeState(),
        context=context,
    )
)
```

## 7. Tool Policy Enforcement

Chat 的禁止规则必须在工具执行边界强制执行，不能只写入 prompt。

建议增加 chat-specific `ToolRegistry` view 或 policy wrapper，而不是修改每个具体工具的实现：

```text
ChatResourceView
  → ChatToolPolicyGuard
  → existing ToolRegistry
  → existing tool implementation
```

Guard 的检查顺序：

1. MCP 工具：交给现有 MCP integration；不做本设计新增的 read/write 分类。
2. 明确拒绝工具名：`bash`、`powershell` 直接拒绝。
3. 本地工具：必须存在 chat allow rule，并满足该工具现有的只读约束。
4. 未知或未声明的非 MCP 工具：默认拒绝。
5. `agent`/subagent：第一版不绑定，直接拒绝，不能通过子 agent 获得未绑定工具。
6. 拒绝必须生成稳定的 tool-denied result，并保留在当前 chat turn 的事件/诊断中；不得静默转换成 coding 权限请求，也不得触发审批流程。

所有允许调用的工具必须在 chat thread 创建时绑定到 tool view；运行时不通过用户审批扩大 allowlist。

Shell 规则按工具 capability 而非命令内容判断：即使请求只是 `bash -c 'cat file'`，chat 也拒绝；不能通过命令分析绕过禁止。

## 8. Prompt and Interaction Policy

Chat prompt 应表达：

- 这是一个 chat profile；
- 可以使用已绑定的本地只读和 Web 工具帮助回答；
- 不得执行 shell 或本地写入；
- MCP 工具按当前 MCP integration 的可用性使用；
- 工具被 policy 拒绝时，应向用户说明限制并继续以无工具方式回答。

Prompt 只是行为指导，最终工具执行由已绑定 tool view 和 `ChatToolPolicyGuard` 强制决定，不产生权限审批请求。

Chat 不复用 coding 的 goal/plan/task persona 默认注入。若未来需要 goal/workflow，应该通过独立 profile/lifecycle 扩展，不把 coding `TaskState` 隐式塞入 chat。

## 9. Lifecycle and Error Semantics

第一版只实现同步单轮：

```text
CREATED → RUNNING → COMPLETED
```

异常：

```text
RUNNING → FAILED
```

取消：

```text
RUNNING → CANCELLED
```

`AgentRuntime` 继续是 turn event 和 runtime state commit 的 owner；LangGraph infrastructure 继续负责其已有的 message/transcript persistence。ChatService 不增加第二次保存。

在发生 policy denial 时，turn 本身不自动失败：

```text
tool request → denied tool result → model continues or returns
```

只有 runtime/tool engine 无法继续执行、显式异常或取消时，才进入 `FAILED`/`CANCELLED`。

## 10. Frontend/API Boundary

本设计不新增具体前端协议，但后续 adapter 必须显式携带：

- `mode/profile_id = chat`；
- `thread_id` 或 `chat_session_id`；
- user text；
- optional context；
- turn result 的最终 `thread_id/session_id/lifecycle`。

不得只通过一个全局 `InteractionMode` 字段推断 chat 的 transcript owner 或工具权限。协议层的 chat identity 必须能够映射到 `AgentThread`。

## 11. Implementation Plan

### Phase 2A. Contracts and policy

- 扩展 `RuntimeProfile`、`TurnRequest` 和 `RuntimeResources`，保持 coding 默认行为不变。
- 定义 `ChatToolPolicy`、`ChatPromptPolicy`、`ChatInteractionPolicy` 或等价不可变契约。
- 为 chat allow/deny 规则增加纯 domain tests。

### Phase 2B. Resource view and persistence

- 增加 chat-scoped resource view/policy guard。
- 接入现有 tool registry、MCP catalog、session store 和 event publisher。
- 增加独立 chat session/transcript persistence；禁止 fallback 到 coding session。

### Phase 2C. Chat application path

- 实现 `ChatService`。
- 复用 `AgentRuntime.run_turn()`，不创建第二套 executor。
- 增加 chat thread 创建、恢复和 lazy identity 返回。

### Phase 2D. Adapter and regression

- 接入需要 chat 的 gateway/frontend adapter。
- 运行 chat focused tests、agent suite 和完整 backend suite。
- 确认 coding regression、MCP 行为和现有 LangGraph topology 不变。

## 12. Acceptance Criteria

### Behavior

- chat 普通问答可以完成一个 turn；
- chat 可以使用允许的本地只读工具；
- chat 可以使用 Web search/fetch；
- chat 可以调用现有 MCP 工具，不因本设计增加 MCP read/write 分类；
- `bash` 和 `powershell` 始终被拒绝，包括只读命令；
- 本地写入、修改、删除、移动和 git 写操作被拒绝；
- `agent`/subagent 默认被拒绝，不能绕过 chat policy。

### Isolation

- chat transcript 不写入 coding session；
- chat runtime state、compaction 和 tool context 不污染 coding thread；
- lazy chat session 的最终 identity 可从 `TurnResult` 获得；
- 同一 chat thread 恢复后能继续自己的上下文。

### Ownership

- production chat 只有 `AgentRuntime.run_turn()` 一个 turn execution entry；
- ChatService 不保存 runtime、消息或 event；
- runtime state 和 turn event 没有重复写入/发布；
- 现有 LangGraph message/transcript persistence 不被复制。

### Tests

至少覆盖：

- `CHAT_PROFILE` contract 和 policy validation；
- allow read-only local tool；
- allow `websearch`/`webfetch`；
- allow MCP tool already present in the bound tool view without adding read/write classification；
- deny unbound tools without starting an approval request；
- deny out-of-scope resources without starting an approval request；
- deny `bash`/`powershell`；
- deny local write/edit/delete/move；
- deny agent/subagent default path；
- chat/coding session isolation；
- ordinary、lazy、exception、cancellation lifecycle；
- event order and runtime save count；
- coding focused regression and full backend suite。

## 13. Forbidden Changes

实施 chat 时禁止：

- 在 `AgentRuntime` 外增加第二套 turn executor；
- 在 `ChatService` 复制 `TurnRunner` 或 LangGraph topology；
- 用 prompt 代替 tool policy enforcement；
- 把 chat transcript/runtime state 存入 coding session；
- 将 MCP 工具强行加入本设计新增的 read/write 分类；
- 允许通过 shell 参数分析绕过 `bash`/`powershell` 禁止；
- 为第一版引入 loop scheduler、goal/workflow、并发 recovery 或 frontend 协议大改。

## 14. Handoff

实现入口和现有基础：

- `src/voidx/agent/runtime/runtime.py`
- `src/voidx/agent/runtime/contracts.py`
- `src/voidx/agent/domain/profile.py`
- `src/voidx/agent/domain/thread.py`
- `src/voidx/agent/domain/state.py`
- `src/voidx/agent/ports/runtime_resources.py`
- `src/voidx/agent/infrastructure/langgraph/adapter.py`
- `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py`

第一期实现计划仍保持 coding scope 完成；本设计是 chat Phase 2A–2D 的设计输入，不代表 chat 已经实现。
