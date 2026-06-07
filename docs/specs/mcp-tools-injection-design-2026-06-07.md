# MCP 工具注入 Agent 设计

> **Status: Draft**

Date: 2026-06-07

## Problem

MCP 工具成功注册到 `ToolRegistry`，但 LLM 无法调用它们。原因是 agent 的 `tools` 白名单是硬编码的，只包含内置工具 ID，不包含 `mcp__` 前缀的动态工具。

过滤逻辑在 `core.py:541-542`：

```python
if agent_tool_ids is not None:
    tool_defs = [t for t in all_tool_defs if t["function"]["name"] in agent_tool_ids]
```

子 agent 同样受影响（`subagent.py:74`）。

## Current Flow

```
McpManager.start_all()
  → client.list_tools()
  → McpToolWrapper 注册到 ToolRegistry (id = "mcp__typex__send_message_abc12345")
  → ToolRegistry._tools 包含 MCP 工具 ✅

_call_llm()
  → agent.tools = ["read", "glob", "bash", ...]  (硬编码白名单)
  → tool_defs = [t for t in all_tool_defs if name in agent_tool_ids]
  → MCP 工具被过滤掉 ❌
  → LLM 看不到 MCP 工具
```

## Design Goals

1. MCP 工具对 orchestrator 默认可见
2. 子 agent 可按需控制是否暴露 MCP 工具
3. 权限系统已正确处理 MCP（`mcp__*` → ask），不需要改动
4. 不破坏现有 agent 工具隔离语义（explore 不应能发消息）

## Approach

在 `AgentDef` 上增加 `mcp_tools: bool` 字段，控制该 agent 是否能使用 MCP 工具。过滤逻辑中，`mcp__` 前缀的工具绕过白名单检查，但受 `mcp_tools` 字段控制。

### AgentDef 变更

```python
class AgentDef(BaseModel):
    name: str
    description: str
    when_to_use: str
    tools: list[str]
    can_write: bool
    can_delegate: bool
    max_steps: int = 25
    hidden: bool = False
    model: str | None = None
    mcp_tools: bool = False  # NEW: whether this agent can use MCP tools
```

### BUILTIN_AGENTS 变更

| Agent | mcp_tools | 理由 |
|-------|-----------|------|
| orchestrator | `True` | 主 agent，需要协调所有能力 |
| explore | `False` | 只读搜索，不应触发外部服务 |
| plan | `False` | 只读设计，不应触发外部服务 |
| implement | `False` | 代码编写，不应触发外部服务 |
| review | `False` | 只读审查，不应触发外部服务 |

子 agent 如果需要 MCP 工具，可以后续按需开启。

### 过滤逻辑变更

`core.py` 中 `_call_llm` 的工具过滤：

```python
agent = get_agent(state.get("agent", "orchestrator"))
agent_tool_ids = agent.tools if agent else None
mcp_allowed = agent.mcp_tools if agent else False
all_tool_defs = self.tools.tools_for_llm()

if agent_tool_ids is not None:
    tool_defs = [
        t for t in all_tool_defs
        if t["function"]["name"] in agent_tool_ids
        or (mcp_allowed and t["function"]["name"].startswith("mcp__"))
    ]
else:
    tool_defs = all_tool_defs
```

`subagent.py` 中子 agent 的工具过滤：

```python
agent_tools = ToolRegistry()
builtin_ids = set(agent_def.tools) - {"agent", "task_status"}
if agent_def.mcp_tools:
    # Include MCP tools from parent registry
    mcp_ids = {tid for tid in parent_registry.ids() if tid.startswith("mcp__")}
    agent_tools.filter_tools(builtin_ids | mcp_ids)
else:
    agent_tools.filter_tools(builtin_ids)
```

子 agent 需要接收 parent 的 `ToolRegistry`（或至少 MCP 工具的实例），才能正确注册和执行 MCP 工具。

### tool_contract 更新

`AgentDef.tool_contract` 属性应反映 MCP 工具可用性：

```python
if self.mcp_tools:
    lines.append("- MCP tools: available (subject to permission approval)")
```

### 权限系统

无需改动。当前权限规则已正确处理：

- `mcp__*` → `MCP_TOOLS` capability
- 默认 action = `ask`（需要用户确认）
- `deny_silent` 仍按 settings 中的 disallow 列表工作

## `/mcp` 命令增强

当前 `/mcp` 支持 `new|list|test|del|restart|tools`，但缺少 `disable`/`enable` 操作。

`McpServerConfig` 已有 `disabled: bool = False` 字段，`McpManager.start_all()` 也已跳过 disabled 的服务器。只需要在 slash 命令和 settings 层暴露操作。

### 新增命令

- `/mcp disable <name>` — 设置 `disabled=True`，重启 MCP 管理器使生效
- `/mcp enable <name>` — 设置 `disabled=False`，重启 MCP 管理器使生效

### Settings 层

新增 `set_mcp_server_disabled(name, disabled) -> Path`：

```python
def set_mcp_server_disabled(self, name: str, disabled: bool) -> Path:
    servers = self._data.get("mcpServers", {})
    if name not in servers:
        raise KeyError(name)
    servers[name]["disabled"] = disabled
    self._save()
    return self._path
```

### Slash 层

在 `_mcp` 的 action 分发中增加 `disable`/`enable`，复用 `_pick_mcp_server` 模式：

- `/mcp disable` — 弹出选择列表
- `/mcp disable typex` — 直接操作
- `/mcp enable` — 弹出选择列表
- `/mcp enable typex` — 直接操作

与 `del`/`test`/`tools` 行为一致。

### 命令面板

更新 usage 提示：`/mcp [new|list|test|del|restart|tools|disable|enable]`

### 与 `/tavily` 的关系

Tavily 有两条使用路径：

1. **内置 HTTP** — `WebSearchTool` 通过 API key 直接调 Tavily REST API，不走 MCP
2. **MCP 委托** — `/mcp new` 选 Tavily 时创建 MCP 服务器，`web.search`/`web.fetch` 路由到 `tavily_search`/`tavily_extract`

**核心改造：`/tavily set` 自动创建 Tavily MCP 服务器配置**

当前问题：用户通过 `/tavily set` 配了 API key，但 Tavily MCP 服务器不存在，`/mcp list` 看不到，`disable/enable` 也管不到。

改造方案：`/tavily set` 保存 key 后，自动执行以下逻辑：

1. 检查 `mcpServers` 中是否已有名为 `tavily` 的服务器
2. 如果没有，自动创建 `McpServerConfig(name="tavily", command="npx", args=["-y", "tavily-mcp@latest"], env={"TAVILY_API_KEY": key}, tools=["tavily_search", "tavily_extract"])` 并 `save_mcp_server()`
3. 自动设置 `web.search` → `tavily/tavily_search`、`web.fetch` → `tavily/tavily_extract` 路由
4. 触发 `manager.restart_all()` 建立 MCP 长连接
5. 提示用户：`Tavily MCP server configured and connected.`

如果已有 `tavily` 服务器，只更新 `env.TAVILY_API_KEY` 并 `restart_all()`。

`/tavily delete` 对应改造：

1. 删除 API key
2. 如果 `tavily` MCP 服务器存在，从 `env` 中移除 `TAVILY_API_KEY`（保留服务器配置，因为可能还有环境变量来源）
3. 触发 `restart_all()`

这样 Tavily MCP 长连接就完全纳入 MCP 管理体系：

- `/mcp list` 能看到 tavily 服务器
- `/mcp disable tavily` 断开 MCP 连接，web search 降级到内置 HTTP（有 key 就用 Tavily API，没有就 DuckDuckGo）
- `/mcp enable tavily` 重连 MCP
- `/mcp test tavily` 测试连接
- `/mcp del tavily` 删除 MCP 服务器配置（API key 保留，降级到内置 HTTP）

`web.search`/`web.fetch` 的路由优先级：MCP 路由 > 内置 Tavily > DuckDuckGo fallback。禁用 MCP 后自动降级。

## Edge Cases

1. **MCP 服务器未连接**：`McpToolWrapper.execute` 已处理，返回错误信息
2. **MCP 工具动态变化**：`McpManager.restart_all()` 会重新注册，下次 LLM 调用自动看到新工具
3. **子 agent 需要特定 MCP 工具**：未来可扩展为 `mcp_tools: list[str]`（指定允许的 MCP 工具名），当前 `bool` 足够
4. **settings 中的 tools 过滤**：`_resolve_tool_filter` 在注册阶段已生效，不在白名单的 MCP 工具根本不会注册到 ToolRegistry
5. **disable 后 MCP 工具从 ToolRegistry 移除**：`restart_all()` 先 `unregister_prefix("mcp__")` 再重新注册，disabled 的服务器不会注册工具，所以工具自动消失

## Tests

| Test | Description |
|------|-------------|
| `test_orchestrator_sees_mcp_tools` | orchestrator 的 tool_defs 包含 `mcp__` 前缀工具 |
| `test_explore_does_not_see_mcp_tools` | explore 的 tool_defs 不包含 `mcp__` 前缀工具 |
| `test_mcp_tools_false_excludes_all_mcp` | `mcp_tools=False` 时所有 `mcp__` 工具被过滤 |
| `test_subagent_with_mcp_tools_includes_mcp` | `mcp_tools=True` 的子 agent 能看到 MCP 工具 |
| `test_subagent_without_mcp_tools_excludes_mcp` | `mcp_tools=False` 的子 agent 看不到 MCP 工具 |
| `test_mcp_tool_execution_requires_permission` | MCP 工具调用仍受权限系统控制 |
| `test_mcp_disable_command_sets_disabled_true` | `/mcp disable <name>` 设置 disabled=True 并重启 |
| `test_mcp_enable_command_sets_disabled_false` | `/mcp enable <name>` 设置 disabled=False 并重启 |
| `test_mcp_disable_removes_tools_from_registry` | disable 后 MCP 工具从 ToolRegistry 移除 |

## Acceptance Criteria

- orchestrator 能看到并调用 MCP 工具
- 子 agent 默认看不到 MCP 工具
- 权限系统对 MCP 工具的 ask/deny 行为不变
- `tool_contract` 反映 MCP 工具可用性
