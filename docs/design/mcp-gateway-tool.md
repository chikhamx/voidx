# MCP Gateway Tool — 技术设计文档

> **Status: Design**
> Date: 2026-07-14

## Context

voidx 当前 MCP 集成会在 MCP server 启动后发现真实工具，并把每个工具封装成 `mcp__{server}__{tool}_{hash}` 注册到 `ToolRegistry`。随后主循环从 registry 生成 `tool_defs`，再传给模型的 `bind_tools()`。

这个模式直接、类型约束强，但有三个代价：

1. **工具列表膨胀**：多个 MCP server 会把几十到上百个工具直接暴露给模型。
2. **模型侧 prefix cache 不稳定**：MCP catalog 一旦新增、删除、重连或过滤，`tool_defs` 变化会让 provider 侧工具 schema 前缀缓存失效。
3. **TUI 可读性差**：原始 MCP tool id 长且带 hash，需要额外压缩渲染。

Skill 工具提供了另一种模式：`skill list/load` 只把按需说明注入当前上下文，不动态改变 bound tools。本设计借鉴 skill 的 `list/load` 体验，但新增一个稳定的 MCP 执行入口。

## Goals

- 保持 bound tool 列表稳定，避免 MCP catalog 变化破坏 prefix cache。
- 用一个固定 `mcp` gateway tool 覆盖 MCP discovery、说明加载和实际执行。
- 支持按需加载 MCP server/tool 的描述、参数摘要和调用示例。
- 保留 MCP manager/client 的连接、调用、状态管理能力。
- 在执行前用真实 MCP input schema 校验参数，降低 gateway 模式下的参数漂移。
- 让 TUI/GUI 渲染真实 MCP 动作，而不是显示笼统的 `mcp(...)`。

## Non-Goals

- 不在第一版继续把每个 MCP tool 暴露成 native function tool。
- 不要求完整替换 `websearch/webfetch`；高频内置路线可继续保留或逐步迁移。
- 不在 `load` 输出里塞完整原始 JSON Schema；优先输出压缩后的可读参数说明。
- 不实现跨进程 MCP catalog 持久缓存；第一版以内存 catalog 为准。

## Proposed Model

新增一个固定内置工具 `mcp`，始终注册在 `ToolRegistry` 中。它的 schema 稳定，不随 MCP server 或 tool 数量变化。

```json
{
  "op": "list | load | call",
  "server": "tavily",
  "tool": "tavily_search",
  "arguments": {},
  "query": "optional discovery query"
}
```

模型使用流程：

1. 需要 MCP 能力时，调用 `mcp(op="list")` 查看可用 server/tool bundle。
2. 需要具体说明时，调用 `mcp(op="load", server="tavily")` 或 `mcp(op="load", server="tavily", tool="tavily_search")`。
3. `load` 返回类似 skill load 的当前 turn 上下文，包含工具列表、用途、参数摘要和示例。
4. 执行时调用 `mcp(op="call", server="tavily", tool="tavily_search", arguments={...})`。
5. `mcp` 工具内部通过 `McpManager.call_tool(server, tool, arguments)` 调真实 MCP server。

## Tool Description Contract

固定 gateway tool 的注册描述需要承担 MCP 工作流指引，不再额外新增 system/runtime section。这样模型在查看 bound tool schema 时就能学到 `list/load/call` 用法，同时避免 prompt 里再重复一层 MCP 指引。

`McpGatewayTool.description` 应包含短规则，但不包含 MCP catalog。Catalog 只能通过 `mcp list/load` 进入当前 turn。

建议工具描述包含：

```text
Discover, load, and call Model Context Protocol (MCP) tools through a stable gateway.

- Use `mcp(op="list")` to discover available MCP servers and tool bundles.
- Use `mcp(op="load", server="...")` before calling an unfamiliar MCP server or tool.
- Use `mcp(op="call", server="...", tool="...", arguments={...})` to execute a real MCP tool.
- Do not invent MCP server or tool names. If uncertain, list or load first.
- Treat `mcp load` output as current-turn context and follow its parameter examples.
```

这层和 skill 机制的关系：

- `skill load` 注入某个能力的操作说明。
- `mcp load` 注入某个 MCP server/tool 的操作说明。
- `mcp` tool description 只告诉模型 gateway 工作流，不列出真实 server/tool。

因为描述属于固定 bound tool schema，只要 `mcp` 工具 schema 不变，它不会因 MCP server 新增、删除或重连而抖动。相比 system prompt，这个位置更贴近工具调用决策，也避免在 runtime context 中重复维护一份 MCP 使用规则。

## Tool Operations

### `list`

返回 MCP server 摘要，控制输出长度。

字段建议：

| 字段 | 说明 |
| --- | --- |
| `server` | MCP server name |
| `status` | connected / connecting / error / disabled |
| `tool_count` | 可用工具数量 |
| `summary` | server 描述或来源 |
| `examples` | 可选，1-2 个代表工具名 |

如果传入 `query`，`list` 可以做简单过滤，用于查找相关 MCP server 或 tool。

### `load`

返回当前 turn 可读上下文，不改变 bound tool 列表。

输出使用独立 marker，便于历史消息 stripping：

```text
VOIDX_MCP_TOOL_CONTEXT
Scope: current-turn

## MCP Server: tavily
Status: connected

Tools:
- tavily_search: Search the web.
  Required: query
  Optional: max_results, search_depth
  Example:
    mcp(op="call", server="tavily", tool="tavily_search", arguments={"query": "..."})
- tavily_extract: Extract page content from URLs.
  Required: urls
  Example:
    mcp(op="call", server="tavily", tool="tavily_extract", arguments={"urls": ["https://..."]})
```

`load` 输出应压缩 schema：

- 只展示 required fields、常用 optional fields、字段描述和 examples。
- 大 schema 超限时只展示前 N 个字段，并提示可加载具体 tool。
- 加载具体 `tool` 时可以展示更完整参数摘要。

### `call`

执行真实 MCP tool。

执行前校验：

- server 存在且 connected。
- tool 存在且未被配置过滤。
- `arguments` 能通过 MCP tool input schema 校验。
- 权限服务允许 `mcp:{server}:{tool}` 或对应策略。

校验失败时返回结构化、可修复错误：

```text
MCP call failed: invalid arguments for tavily/tavily_search.
Missing required field: query.
Run mcp(op="load", server="tavily", tool="tavily_search") for parameter details.
```

## Architecture

### Components

| Component | Responsibility |
| --- | --- |
| `McpGatewayTool` | 固定 bound tool，处理 `list/load/call` |
| `McpManager` | 继续管理 MCP client 生命周期、状态、真实调用 |
| `McpCatalog` | 从 connected clients 读取并缓存 server/tool definitions |
| `McpContextRenderer` | 把 MCP definitions 渲染成 skill-like current-turn context |
| `McpArgumentValidator` | 用 input schema 校验 `call.arguments` |
| `McpGatewayTool.description` | 提供固定工具描述，教模型使用 gateway 工作流 |
| UI display helpers | 把 gateway call 渲染成 `Tavily Search("query")` |

### Data Flow

```text
LLM
  -> mcp(op=list/load/call)
    -> McpGatewayTool
      -> McpCatalog / McpContextRenderer
      -> PermissionService
      -> McpManager.call_tool()
        -> McpClient
          -> real MCP server
```

`ToolRegistry.tools_for_llm()` 只看到稳定的 `mcp` tool。真实 MCP catalog 不进入 `tool_defs`，而是作为 tool result 或内部 runtime state 使用。

## Prefix Cache Behavior

目标是让 provider 侧请求前缀尽量稳定：

- bound tools 固定，不随 MCP catalog 变化。
- system/runtime context 不包含 MCP catalog，也不需要额外 MCP 指引。
- 固定 `mcp` tool description 承载 gateway 工作流，不随 MCP catalog 变化。
- `mcp load` 的大段说明只出现在当前 turn 的 tool result 中，历史中可被 marker stripping 压缩。
- 新增/删除 MCP server 不改变 `bind_tools()` schema，只改变 `mcp list/load` 返回内容。

这比动态注册 MCP native tools 更适合长会话和大量 MCP server。

## Permissions

权限模型建议引入 MCP 资源名：

```text
mcp:{server}:{tool}
```

示例：

- `mcp:tavily:tavily_search`
- `mcp:github:create_issue`
- `mcp:*:*`

权限 prompt 展示应使用真实动作：

```text
MCP: Tavily Search
Arguments: query="..."
```

第一版可沿用现有 MCP wildcard 策略，但执行层应统一从 gateway call 中提取 `server/tool/arguments`，不要只审批 `mcp` 这个固定工具名，否则权限粒度会过粗。

## UI Rendering

TUI/GUI 不应显示 `Mcp("call")`。对 `mcp(op="call", server, tool, arguments)`：

- 标题显示为 `{Server} {Tool}("display value")`。
- display value 复用现有工具摘要逻辑：优先 `query/url/urls/path/pattern/name/text`。
- `urls` 列表显示为 `first +N more`。
- `op=list/load` 可显示为 `MCP List()`、`MCP Load("tavily")`。

示例：

```text
● Tavily Search("goal mode mechanism")
● Tavily Extract("https://example.com +2 more")
● MCP Load("github")
```

## Error Handling

| 场景 | 行为 |
| --- | --- |
| MCP manager unavailable | 返回可读错误，提示配置 MCP server |
| server connecting | 返回状态并提示稍后重试 |
| server disconnected | 尝试 reconnect；失败则返回连接错误 |
| unknown server/tool | 返回可用候选或提示 `mcp list` |
| invalid arguments | 返回 schema 校验错误和 `load` 提示 |
| MCP tool error | 保留 MCP error 内容，标记 metadata.error |
| large result | 走现有 display policy summary / truncation |

## Migration Plan

1. 新增 `McpGatewayTool`，注册为内置工具 `mcp`。
2. 提取 `McpCatalog`，让 `McpManager` 能提供 server/tool defs 给 gateway。
3. 在 `McpGatewayTool.description` 中写清 `list/load/call` 工作流。
4. 实现 `list/load`，先不改变现有 direct MCP tool 注册。
5. 实现 `call`，复用 `McpManager.call_tool()` 和 `format_mcp_call_result()`。
6. 加入参数 schema 校验和权限分类。
7. 更新 TUI/GUI 渲染，使 gateway call 显示为真实 MCP 动作。
8. 增加配置开关：
   - `mcp.exposure = "gateway"`：只暴露固定 `mcp` tool。
   - `mcp.exposure = "direct"`：保留当前每工具注册模式。
   - `mcp.exposure = "hybrid"`：固定 gateway + 少量显式 allowlist direct tools。
9. 默认切到 `gateway` 或先在实验配置中启用，观察 cache hit、调用成功率和用户反馈。

## Testing

### Unit Tests

- `mcp list` 返回 server 状态、tool_count、过滤后的工具摘要。
- `mcp load server` 返回 marker、工具描述、参数摘要和 examples。
- `mcp load server/tool` 返回单工具详细参数摘要。
- `mcp call` 对 valid arguments 调用 `McpManager.call_tool()`。
- `mcp call` 对 missing required field 返回可修复错误。
- 权限分类能按 `mcp:{server}:{tool}` 做 allow/ask/deny。
- 历史 stripping 能把 `VOIDX_MCP_TOOL_CONTEXT` 压缩为摘要。
- `McpGatewayTool.description` 固定且不包含 server/tool catalog。

### Integration Tests

- 主 LLM call 的 `tool_defs` 在 MCP server 新增/删除后仍保持稳定。
- 主 LLM call 的 stable system prefix 不受 MCP catalog 变化影响。
- direct 模式与 gateway 模式都能调用同一个 fake MCP tool。
- 子代理继承固定 `mcp` tool，但不继承膨胀后的 direct MCP tools。
- TUI 渲染 `mcp call` 为真实动作名称。

### Regression Metrics

- prefix cache read/write tokens。
- MCP tool call success rate。
- invalid argument retry rate。
- tool_defs token count。

## Open Questions

- 默认 exposure 应直接切 `gateway`，还是先提供实验开关？
- `load` 是否允许一次加载所有 connected MCP server，还是强制按 server 加载？
- 参数 schema 压缩是否需要保留 enum、array item 类型和 nested object path？
- `websearch/webfetch` 是否继续作为高频 native tools，还是迁移到 gateway-backed wrappers？
- 是否需要 `mcp(op="search", query="...")` 作为 `list` 的语义别名，方便模型发现工具？

## Recommended First Version

第一版建议做保守实现：

1. 新增固定 `mcp` tool，支持 `list/load/call`。
2. 在 `mcp` 工具描述里教模型先 `list/load` 再 `call`。
3. 保留现有 direct MCP 注册，但加配置 `mcp.exposure`。
4. 默认仍可先用现状，开发者开启 `gateway` 测试。
5. 在 gateway 模式下禁用 direct `mcp__*` 注册，只保留固定 `mcp`。
6. 收集 cache hit 和调用错误数据后，再决定是否默认切换。

这个路径风险低：不会一次性删除现有 MCP 能力，同时能验证 gateway 是否真的改善 cache 和工具列表膨胀问题。
