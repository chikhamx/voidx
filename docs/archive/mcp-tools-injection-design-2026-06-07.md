# MCP 工具注入 Agent 设计

> **Status: Done**

Date: 2026-06-07

## Problem

MCP 工具已经能通过 `McpManager.start_all()` 注册到主 `ToolRegistry`，但 LLM 仍看不到这些工具。根因是 agent 的工具可见性由硬编码 `AgentDef.tools` 白名单控制，只包含内置工具 ID，不包含 `mcp__` 前缀的动态工具。

当前主 agent 过滤逻辑：

```python
if agent_tool_ids is not None:
    tool_defs = [t for t in all_tool_defs if t["function"]["name"] in agent_tool_ids]
```

子 agent 也受影响：`run_subagent()` 会新建一个只含内置工具的 `ToolRegistry()`，所以即使把 `mcp__` id 加进白名单，子 agent registry 里也没有对应的 MCP wrapper 实例。

## Goals

1. Orchestrator 默认能看到已注册的 MCP 工具。
2. 子 agent 默认看不到 MCP 工具，但以后可按 agent 显式开启。
3. MCP 工具执行仍走现有权限系统，默认 `ask`。
4. 不破坏 agent 工具隔离：read-only 子 agent 不应因为 MCP 开关获得外部副作用工具。
5. `/mcp enable|disable` 和 `/tavily set` 相关体验作为后续阶段，不阻塞核心 bugfix。

## Phases

### Phase 1: Agent MCP Tool Visibility

核心修复只解决 “LLM 看不到 MCP 工具”。

#### AgentDef

新增 `mcp_tools: bool = False`：

```python
class AgentDef(BaseModel):
    ...
    mcp_tools: bool = False
```

内置 agent 设置：

| Agent | mcp_tools | 理由 |
|-------|-----------|------|
| orchestrator | `True` | 主 agent 负责协调用户任务，需要看到配置好的外部 MCP 能力 |
| explore | `False` | read-only 子 agent 不应触发外部服务副作用 |
| plan | `False` | 设计阶段保持只读和本地分析 |
| implement | `False` | 代码执行 agent 不默认访问外部服务 |
| review | `False` | 审查默认只读，不默认访问外部服务 |
| compaction/title | `False` | 内部 agent 无工具 |

`tool_contract` 增加一行：

```text
- MCP tools: available when configured; each call is permission-gated
```

只有 `mcp_tools=True` 的 agent 才显示该行。`child_agent_descriptions_for_llm()` 也应在未来某个子 agent 开启 MCP 时显示对应能力，避免 orchestrator 误判子 agent 能力。

#### Main Agent Filtering

主 agent 从 `self.tools.tools_for_llm()` 获取全部已注册工具，过滤时允许 `mcp__` 工具绕过静态白名单，但必须受 `agent.mcp_tools` 控制：

```python
mcp_allowed = bool(agent and agent.mcp_tools)
tool_defs = [
    t for t in all_tool_defs
    if name in agent_tool_ids or (mcp_allowed and name.startswith("mcp__"))
]
```

`available_tool_ids` 仍作为更后置、更严格的运行时可见性过滤。也就是说，即使 `mcp_tools=True`，如果当前 state 显式限制了 `available_tool_ids`，MCP 工具仍必须在该集合内才可见。

#### Subagent Registry

子 agent 不能只把 parent 的 MCP id 加入 allowlist；它必须实际拿到 parent registry 中的 MCP wrapper 实例。

实现方式：

1. `ToolRegistry` 增加 `filtered_copy(allowed_ids)`，复制已有 `ToolDef` 和 tool instance，不重新实例化工具。
2. `run_subagent()` 新增 `parent_tools: ToolRegistry | None = None`。
3. 子 agent 的 base registry 使用 `parent_tools`，没有 parent 时回退到 `ToolRegistry()`。
4. `allowed_ids = set(agent_def.tools) - {"agent", "task_status"}`。
5. 如果 `agent_def.mcp_tools=True` 且有 parent registry，则加入所有 `parent_tools.ids()` 中的 `mcp__` 工具。
6. `agent_tools = base_registry.filtered_copy(allowed_ids)`。

这样子 agent 默认隔离，未来某个 agent 显式开启 `mcp_tools=True` 时，既能看到 MCP tool def，也能执行对应 wrapper。

#### Permissions

权限系统不改。现有规则已覆盖：

- `capability_for_tool("mcp__...")` → `PermissionCapability.MCP_TOOLS`
- `BASIC_RULES` 中 `mcp__*` 默认 `ask`
- session allow/deny wildcard 支持 `mcp__server__*` 和 `mcp/server/*`

Phase 1 只补执行路径测试，证明可见工具真正进入权限审批流程。

#### Phase 1 Tests

| Test | Description |
|------|-------------|
| `test_orchestrator_sees_mcp_tools` | orchestrator 的 tool defs 包含 `mcp__` 工具 |
| `test_non_mcp_agent_does_not_see_mcp_tools` | `mcp_tools=False` 的 agent 看不到 MCP 工具 |
| `test_available_tool_ids_can_hide_mcp_tools` | runtime `available_tool_ids` 仍可隐藏 MCP 工具 |
| `test_subagent_without_mcp_tools_excludes_parent_mcp_tools` | 子 agent 默认不复制 parent MCP 工具 |
| `test_subagent_with_mcp_tools_copies_parent_mcp_tools` | 显式开启 `mcp_tools=True` 时复制 parent MCP wrapper |
| `test_mcp_tool_execution_requires_permission` | LLM 调用 MCP 工具时仍进入 permission ask 流 |

### Phase 2: `/mcp enable|disable`

这是 MCP 管理体验增强，不是核心可见性修复。

新增命令：

- `/mcp disable <name>`：设置 `McpServerConfig.disabled=True`，重启 MCP manager。
- `/mcp enable <name>`：设置 `disabled=False`，重启 MCP manager。
- 无 target 时复用 `_pick_mcp_server()` 选择列表。

Settings 层新增：

```python
set_mcp_server_disabled(name: str, disabled: bool) -> Path
```

禁用服务器后，`McpManager.restart_all()` 会 `unregister_prefix("mcp__")` 并跳过 disabled server，因此对应 MCP wrapper 会从 registry 消失。

#### Web Route Fallback

当前 `websearch`/`webfetch` 在 route 为 MCP 时，如果 MCP server 不可用，会返回 error `ToolResult`，不会降级。Phase 2 需要明确 disable 的降级语义：

- 如果禁用某 server，应清理指向该 server 的 `web.search`/`web.fetch` MCP route。
- 这样 `websearch`/`webfetch` 会自然回退到内置 Tavily/DuckDuckGo 或本地 fetch。
- `/mcp enable` 不自动恢复旧 route；用户可通过 `/mcp new` 或后续 Tavily 同步逻辑重新设置 route。

#### Phase 2 Tests

| Test | Description |
|------|-------------|
| `test_settings_set_mcp_server_disabled` | settings 能切换 disabled 字段 |
| `test_mcp_disable_command_sets_disabled_true_and_restarts` | slash disable 写配置并重启 |
| `test_mcp_enable_command_sets_disabled_false_and_restarts` | slash enable 写配置并重启 |
| `test_mcp_disable_clears_web_routes_for_server` | disable server 时清理指向该 server 的 web routes |
| `test_mcp_disable_removes_tools_from_registry` | restart 后 disabled server 工具不再注册 |

### Phase 3: Tavily API Key Synchronizes Tavily MCP Config

这是 Tavily 专用便利功能，独立于 MCP 工具注入。

`/tavily set` 保存 API key 后，自动确保名为 `tavily` 的 MCP server 配置存在：

```python
McpServerConfig(
    name="tavily",
    command="npx",
    args=["-y", "tavily-mcp@latest"],
    env={"TAVILY_API_KEY": key},
    tools=["tavily_search", "tavily_extract"],
)
```

行为：

1. 如果 `tavily` server 不存在，创建它。
2. 如果已存在，只更新 `env.TAVILY_API_KEY`，保留用户已有 command/args/tools/disabled 配置。
3. 设置 `web.search` → `tavily/tavily_search`，`web.fetch` → `tavily/tavily_extract`。
4. 如果 MCP manager 存在，触发 `restart_all()`。

`/tavily delete`：

1. 删除内置 Tavily API key。
2. 如果 `tavily` MCP server 存在，从 server env 中移除 `TAVILY_API_KEY`，保留 server 配置。
3. 清理指向 `tavily` 的 web routes。
4. 如果 MCP manager 存在，触发 `restart_all()`。

这会让 Tavily MCP 纳入 `/mcp list|disable|enable|del` 管理体系。若用户禁用 Tavily MCP，Phase 2 的 route 清理会让 web search 回退到内置路径。

#### Phase 3 Tests

| Test | Description |
|------|-------------|
| `test_tavily_set_creates_mcp_server_and_routes` | `/tavily set` 自动创建 Tavily MCP 配置和 routes |
| `test_tavily_set_updates_existing_mcp_server_env` | 已有 tavily server 时只更新 env key |
| `test_tavily_delete_removes_key_from_mcp_server_env_and_routes` | `/tavily delete` 移除 env key 并清理 routes |
| `test_tavily_set_restarts_mcp_manager_when_available` | manager 存在时触发 restart |

## Acceptance Criteria

- Phase 1 完成后：orchestrator 能看到并调用已注册 MCP 工具，子 agent 默认看不到，权限行为不变。
- Phase 2 完成后：用户能通过 `/mcp enable|disable` 管理 server，disable 后 registry 和 web routes 都反映禁用状态。
- Phase 3 完成后：`/tavily set/delete` 会同步 Tavily MCP server 配置、web routes 和 MCP manager lifecycle。

## Implementation Result

- `AgentDef.mcp_tools` 已加入，orchestrator 默认开启，子 agent 默认关闭。
- 主 agent tool filtering 已允许 `mcp__` 动态工具在 `mcp_tools=True` 时通过，并保留 `available_tool_ids` 后置限制。
- 子 agent 通过 parent `ToolRegistry.filtered_copy()` 派生工具视图，显式开启 `mcp_tools=True` 时可复用 parent MCP wrapper 实例。
- `/mcp enable|disable` 已接入 settings 和 MCP manager restart；disable 会清理指向该 server 的 web routes。
- `/tavily set/delete` 已同步 Tavily MCP server、web routes 和 MCP manager restart。
- 已补对应 config、slash、graph、subagent 和 permission 测试。
