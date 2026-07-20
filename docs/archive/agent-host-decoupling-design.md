> **Status: Done** — Archived on 2026-07-20.

---
name: agent-host-decoupling-design
display_name: Agent Host 解耦设计：消除 LangGraphExecution 隐式耦合
description: 将 AgentService、SlashHandler、runtime 组件对 LangGraphExecution 私有字段的直接访问收敛到显式端口，使执行后端可替换
doc_type: design
audience: human+llm
status: draft
---

# Agent Host 解耦设计

## 问题

端口驱动重构删除了旧 Graph/Host/Mixin，但 `LangGraphExecution` 仍是事实上的 Host。
三个消费者直接读写它的私有字段，形成隐式耦合：

| 消费者 | 耦合点 | 证据 |
|--------|--------|------|
| `AgentService` | 66 处 `self._execution._xxx` 私有访问 | `application/agent_service.py` |
| `LangGraphStateMapper` | 4 个私有字段读写 | `infrastructure/langgraph/state_mapper.py:13-16,25-28` |
| `LangGraphTurnEngine` | 持有 execution 引用，双入口分支 | `infrastructure/langgraph/adapter.py:33-44` |
| `SlashHandler` | `self.host.` 已走公开接口（24 个属性） | `slash/handler.py` |
| runtime 组件 | `self.host` 持有完整 execution | `turn_runner.py`、`session_runtime.py` 等 |

`AgentService` 的 66 处私有访问集中在 5 个职责簇：

```
28  _ui              ← UI 端口
14  _session         ← 会话端口
 8  _settings        ← 配置端口
 6  _workspace      ← 配置端口
 3  _permission      ← 权限端口
 3  _mcp_manager     ← 外部服务管理器
 2  _restore_transcript_snapshot  ← 会话端口
 1  _usage_stats / _slash / _plan_mode / _debug / _lsp_manager / _gateway_session / _app / _any_messages_sent
```

`LangGraphExecution` 已有公开 property（`session`、`settings`、`permission`、`workspace`、`mcp_manager`、`lsp_manager`、`app`、`usage_stats`、`debug_enabled`、`task_state`），但 `AgentService` 没有使用它们。

## 目标

1. `AgentService` 不再访问任何 `self._execution._xxx` 私有字段。
2. `LangGraphStateMapper` 通过公开端口而非私有字段读写运行时状态。
3. `LangGraphTurnEngine` 消除 `runner.run_once` / `execution.run_turn` 双入口。
4. 替换 `LangGraphExecution` 时只需实现端口，无需改动 `AgentService`。

## 设计

### 策略：收敛到已有公开接口 + 补齐缺失端口

`LangGraphExecution` 已有大部分公开 property。核心问题是 `AgentService` 绕过它们。
因此不需要大规模新增端口，而是：

1. **补齐缺失的公开接口**：为 `AgentService` 需要但 `LangGraphExecution` 尚未公开的字段添加 property 或方法。
2. **定义 `ExecutionHost` Protocol**：把 `AgentService` 依赖的公开接口收敛为一个 Protocol，作为类型契约。
3. **替换私有访问**：把 66 处 `self._execution._xxx` 改为 `self._execution.xxx`（公开 property）。
4. **统一 TurnEngine 入口**：移除 `LangGraphTurnEngine` 的 `runner` 参数，只保留 `execution.run_turn`。

### ExecutionHost Protocol

`AgentService` 需要的接口（按职责簇）：

```python
# src/voidx/agent/ports/execution_host.py

class ExecutionHost(Protocol):
    """AgentService 依赖的执行后端公开接口。"""

    # 配置
    @property
    def config(self) -> Config: ...
    @property
    def workspace(self) -> str: ...
    @property
    def settings(self) -> Settings | None: ...
    @property
    def model(self) -> Any: ...  # LLM model handle

    # 会话
    @property
    def session(self) -> SessionInfo | None: ...
    @property
    def session_id(self) -> str: ...
    async def restore_runtime_state(self) -> None: ...
    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool: ...
    async def delete_empty_current_session(self) -> None: ...
    async def clear_current_session(self) -> None: ...
    async def resume_session(self, session: SessionInfo) -> None: ...
    async def set_session_title(self, title: str) -> None: ...

    # UI
    @property
    def ui(self) -> RuntimeUi: ...  # 已有 runtime_ui_port
    def bind_startup_presenter(self, presenter) -> None: ...
    async def show_startup(self, *, append_transcript: bool, prefer_direct: bool) -> None: ...

    # 运行时状态
    @property
    def task_state(self) -> TaskState: ...
    @property
    def interaction_mode(self) -> InteractionMode: ...
    @property
    def debug_enabled(self) -> bool: ...
    @property
    def plan_mode(self) -> bool: ...
    @property
    def usage_stats(self) -> UsageStats: ...
    def runtime_snapshot(self) -> AgentRuntime: ...
    def set_interaction_mode(self, mode: str | InteractionMode) -> InteractionMode: ...
    def set_task_state(self, task_state: TaskState) -> None: ...
    def submit_guidance(self, text: str, **kwargs) -> bool: ...
    @property
    def any_messages_sent(self) -> bool: ...
    @any_messages_sent.setter
    def any_messages_sent(self, value: bool) -> None: ...
    def reset_message_tracking(self) -> None: ...

    # 权限
    @property
    def permission(self) -> PermissionService: ...

    # 外部服务
    @property
    def mcp_manager(self) -> Any: ...
    @property
    def lsp_manager(self) -> Any: ...

    # Slash
    @property
    def slash(self) -> SlashHandler: ...

    # 网关
    @property
    def gateway_session(self) -> GatewaySession | None: ...

    # 持久化
    async def persist_runtime_state(self) -> None: ...
    async def compact_session_history(self, *, force: bool = True) -> bool: ...
```

### LangGraphExecution 补齐的公开接口

当前缺失、需要从 `_xxx` 提升为公开 property 的字段：

| 私有字段 | 新增公开接口 | 说明 |
|----------|-------------|------|
| `_ui` | `ui` | 返回 `runtime_ui_port` |
| `_workspace` | 已有 `workspace` | 直接使用 |
| `_settings` | 已有 `settings` | 直接使用 |
| `_session` | 已有 `session` | 直接使用 |
| `_permission` | 已有 `permission` | 直接使用 |
| `_mcp_manager` | 已有 `mcp_manager` | 直接使用 |
| `_lsp_manager` | 已有 `lsp_manager` | 直接使用 |
| `_usage_stats` | 已有 `usage_stats` | 直接使用 |
| `_debug` | 已有 `debug_enabled` | 直接使用 |
| `_plan_mode` | 新增 `plan_mode` property | |
| `_slash` | 新增 `slash` property | |
| `_gateway_session` | 新增 `gateway_session` property | |
| `_app` | 已有 `app` | 直接使用 |
| `_restore_runtime_state` | 新增 `restore_runtime_state`（async），当前为 `_restore_runtime_state` | 去下划线公开 |
| `_restore_transcript_snapshot` | 已有 `restore_transcript_snapshot`（async） | 直接使用 |
| `_delete_empty_current_session` | 新增 `delete_empty_current_session`（async），当前为 `_delete_empty_current_session` | 去下划线公开 |
| `_any_messages_sent` | 新增 `any_messages_sent` 可写 property + `reset_message_tracking()` 方法 | `AgentService:151` 写入 `= False`，需 setter |

### LangGraphStateMapper 改造

```python
class LangGraphStateMapper:
    def apply_runtime(self, target: ExecutionHost, runtime: AgentRuntime) -> None:
        target.set_interaction_mode(runtime.interaction_mode)
        target.set_task_state(runtime.task_state)
        target.set_compaction_summary(runtime.compaction_summary)  # 新增
        target.set_session_date(runtime.session_time)  # 新增

    def runtime_from_execution(
        self, source: ExecutionHost, *, turn_phase: TurnPhase
    ) -> AgentRuntime:
        return AgentRuntime(
            interaction_mode=source.interaction_mode,
            task_state=source.task_state.model_copy(deep=True),
            compaction_summary=source.compaction_summary,  # 新增 property
            session_time=source.session_date,  # 新增 property
            turn_phase=turn_phase,
        )
```

需要在 `LangGraphExecution` 新增：`compaction_summary` property、`session_date` property、`set_compaction_summary()`、`set_session_date()`。

### LangGraphTurnEngine 统一入口

移除 `runner` 参数，只保留 `execution.run_turn`：

```python
class LangGraphTurnEngine:
    def __init__(self, execution: ExecutionHost, *, mapper: LangGraphStateMapper | None = None) -> None:
        self._execution = execution
        self._mapper = mapper or LangGraphStateMapper()

    async def run(self, user_text, runtime, *, display_text=None, context=None) -> AgentRuntime:
        self._mapper.apply_runtime(self._execution, runtime)
        await self._execution.run_turn(user_text, display_text=display_text, context=context)
        return self._mapper.runtime_from_execution(self._execution, turn_phase=TurnPhase.COMMITTED)
```

测试中 `runner=FakeRunner()` 改为在 `execution.run_turn` 上 mock。

## 实施任务

### Task 1: LangGraphExecution 补齐公开接口
- [ ] 新增 property：`ui`、`plan_mode`、`slash`、`gateway_session`、`any_messages_sent`、`compaction_summary`、`session_date`
- [ ] 新增 async 方法：`delete_empty_current_session()`
- [ ] 新增 setter：`set_compaction_summary()`、`set_session_date()`
- [ ] 文件：`src/voidx/agent/infrastructure/langgraph/execution.py`
- [ ] 测试：`./test.py --backend -- src/tests/test_agent/infrastructure/langgraph/`

### Task 2: 定义 ExecutionHost Protocol
- [ ] 创建 `src/voidx/agent/ports/execution_host.py`
- [ ] 文件：`src/voidx/agent/ports/execution_host.py`
- [ ] 测试：`./test.py --backend -- src/tests/test_agent/test_module_boundaries.py`

### Task 3: AgentService 替换私有访问
- [ ] 把 66 处 `self._execution._xxx` 改为 `self._execution.xxx`
- [ ] `AgentService.__init__` 类型注解改为 `ExecutionHost`
- [ ] 文件：`src/voidx/agent/application/agent_service.py`
- [ ] 测试：`./test.py --backend -- src/tests/test_agent/graph/`
- [ ] 验证：`grep -c "self\._execution\._" src/voidx/agent/application/agent_service.py` 必须为 0

### Task 4: LangGraphStateMapper 改用公开接口
- [ ] `apply_runtime` / `runtime_from_execution` 改用公开 property 和 setter
- [ ] 文件：`src/voidx/agent/infrastructure/langgraph/state_mapper.py`
- [ ] 测试：`./test.py --backend -- src/tests/test_agent/infrastructure/langgraph/test_adapter.py`

### Task 5: LangGraphTurnEngine 统一入口
- [ ] 移除 `runner` 参数和双入口分支
- [ ] 文件：`src/voidx/agent/infrastructure/langgraph/adapter.py`
- [ ] 测试：`./test.py --backend -- src/tests/test_agent/infrastructure/langgraph/test_adapter.py src/tests/test_agent/infrastructure/langgraph/test_graph_wiring.py`

### Task 6: 全量验证
- [ ] `./test.py --backend`
- [ ] `grep -RIn "self\._execution\._" src/voidx/agent/application/` 必须为 0
- [ ] `grep -RIn "source\._\|target\._" src/voidx/agent/infrastructure/langgraph/state_mapper.py` 必须为 0

## 约束

- **不新增兼容层**：不保留任何私有字段访问的 fallback 或 getattr 兼容。
- **不改动 runtime 组件内部**：`TurnRunner`、`SessionRuntime`、`CompactionCoordinator` 等仍持有 `host: Any`，这是 infrastructure 内部实现，不在本次范围。
- **不改动 SlashHandler**：它已通过 `self.host.` 公开接口访问，无需改动。
- **保留 LangGraphExecution 的 property**：不删除已有公开接口，只新增缺失的。

## 风险

1. **`_ui` 是 `runtime_ui_port` 单例**：公开为 `ui` property 后，`AgentService` 直接调 `self._execution.ui.xxx`，语义不变但调用链更浅。
2. **`_any_messages_sent` 是运行时标志**：需确认它是否被 `AgentService` 真正使用，还是只在 `LangGraphExecution` 内部。如果只在内部，不公开。
3. **测试夹具**：`run_loop_helpers.py` 的 `SimpleNamespace` fake execution 需要补齐新增的公开属性。
